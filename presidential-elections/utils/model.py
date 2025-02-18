import json
from typing import Dict, List, Tuple
from urllib.request import urlopen

import arviz
import numpy as np
import pandas as pd
import pymc3 as pm
from utils.gpapproximation import make_gp_basis
from utils.zerosumnormal import ZeroSumNormal

# Aesara will replace Theano in PyMC 4.0
if pm.math.erf.__module__.split(".")[0] == "theano":
    import theano.tensor as aet
else:
    import aesara.tensor as aet


def dates_to_idx(timelist, reference_date):
    """Convert datetimes to numbers in reference to reference_date"""
    t = (reference_date - timelist) / np.timedelta64(1, "D")
    return np.asarray(t)


def standardize(series):
    """Standardize a pandas series"""
    return (series - series.mean()) / series.std()


class PresidentialElectionsModel:
    """A model for the French 2022 presidential elections.

    Poll and election results are modeled using a Dirichlet Multinomial
    regression. We build upon the idea in [1,2]_ to use a backwards random walk
    to link the prediction for the election results and the observed poll
    results. The dependence between the succesive values of the latent
    popularity is modeled using a multivariate gaussian process with a diagonal
    covariance metic.

    We pool information from past elections by assigning candidates to a set of
    political families and putting a hierarchical model over these families.

    References
    ----------
    .. [1]: Linzer, Drew A. “Dynamic Bayesian Forecasting of Presidential Elections in the States.”
            Journal of the American Statistical Association 108, no. 501 (2013): 124–134.
    .. [2]: Stoetzer, Lukas F., Marcel Neunhoeffer, Thomas Gschwend, Simon Munzert, and Sebastian Sternberg.
            “Forecasting Elections in Multiparty Systems: A Bayesian Approach Combining Polls and Fundamentals.”
            Political Analysis 27, no. 2 (2019): 255–262.

    """

    political_families = [
        "farleft",
        "left",
        "green",
        "center",
        "right",
        "farright",
        "souverainistes",
        "other",
    ]

    def __init__(
        self,
        election_date: str,
        timescales: List[int] = [5, 14, 28],
        weights: List[float] = None,
        test_cutoff: pd.Timedelta = None,
    ):
        """
        Initialize the model builder.

        Parameters
        ----------
        election_date
            Date of the election whose result we want to predict.
        timescales
            The typical number of days over which the opinion is assumed to
            move. We usually use several Gaussian Processes that each correspond
            to a different timescale (in days).
        weights
            The weight to give to each timescale. Defaults to each timescale
            having the same weight.
        test_cutoff
            How much of the dataset for ``election_to_predict`` we want to cut to test the model.
            If 2 months for instance, the last two months of polls in the campaign won't be fed to
            the model.
        """

        self.gp_config = {
            "lengthscale": timescales,
            "kernel": "gaussian",
            "zerosum": True,
            "variance_limit": 0.95,
            "variance_weight": weights,
        }

        polls = self._load_polls()
        (
            self.polls_train,
            self.polls_test,
            self.results_raw,
            self.results_mult,
        ) = self._clean_polls(polls, test_cutoff)

        _, self.unique_elections = self.polls_train["dateelection"].factorize()
        _, self.unique_pollsters = self.polls_train["sondage"].factorize()
        self.results_oos = self.results_mult[
            self.results_mult.dateelection != election_date
        ].copy()

        self._load_predictors()
        (
            self.results_preds,
            self.campaign_preds,
        ) = self._standardize_continuous_predictors()

    def _load_polls(self) -> pd.DataFrame:
        old_polls = self._load_old_polls()
        new_polls = self._load_2022_polls()

        polls = (
            pd.concat([old_polls, new_polls], axis=0)
            .sort_values(["dateelection", "date", "sondage", "samplesize"])
            .reset_index(drop=True)
        )
        polls["nbsouverainistes"] = polls["nbsouverainistes"].fillna(0)
        polls["date"] = pd.to_datetime(polls["date"])

        # add empty line for 2022 results
        polls = polls.set_index("date")
        polls.loc["2022-04-10"] = np.NaN
        polls.loc["2022-04-10", "dateelection"] = pd.to_datetime("2022-04-10")
        polls.loc["2022-04-10", "sondage"] = "result"

        return polls.reset_index()

    @staticmethod
    def _load_old_polls() -> pd.DataFrame:
        polls = pd.read_csv(
            "https://raw.githubusercontent.com/pollsposition/data/main/sondages"
            "/tour1_complet_unitedfl.csv",
            index_col=0,
            parse_dates=["dateelection", "date"],
        )

        # only president elections after 2002
        polls = polls[(polls.date >= "2002-01") & (polls.type == "president")].drop(
            [
                "type",
                "abstention",
                "undecided",
            ],
            axis=1,
        )

        # no green party candidate in 2017
        polls.loc[polls["dateelection"] == "2017-04-23", "nbgreen"] = 0

        return polls.sort_values(
            ["dateelection", "date", "sondage", "samplesize"]
        ).reset_index(drop=True)

    def _load_2022_polls(self) -> pd.DataFrame:
        url = "https://raw.githubusercontent.com/pollsposition/data/main/sondages/presidentielles_2022.json"
        response = urlopen(url)
        raw_polls = json.loads(response.read())
        new_polls = self._clean_up_json(raw_polls)
        return self._format_2022_polls(new_polls)

    def _clean_up_json(self, raw_polls: pd.DataFrame) -> pd.DataFrame:
        metadata = [
            pd.json_normalize(raw_polls["sondages"][poll])[
                ["institut", "date_debut", "date_fin", "premier_tour"]
            ]
            for poll in raw_polls["sondages"].keys()
        ]
        metadata = pd.concat(metadata).sort_values("date_debut")
        metadata[["date_debut", "date_fin"]] = metadata[
            ["date_debut", "date_fin"]
        ].apply(pd.to_datetime)
        metadata = metadata[metadata.date_debut >= "2022-01-01"].reset_index(drop=True)

        polls_temp = []
        for _, row in metadata.iterrows():
            poll = row["premier_tour"]
            polls_temp.append(self.select_hypothesis(poll))
        polls_temp = pd.concat(polls_temp).reset_index(drop=True)

        # exclude certitude
        new_polls = pd.concat([metadata, polls_temp], axis=1).drop(
            ["premier_tour", "base", "nspp", "hypothese"], axis=1
        )
        return new_polls.drop(new_polls.filter(regex="certitude.").columns, axis=1)

    @staticmethod
    def select_hypothesis(poll: List) -> pd.DataFrame:
        """
        Select hypothesis with Taubira when present.
        Just return poll otherwise.
        """
        for hypothesis in poll:
            intentions = hypothesis["intentions"]
            if "Christiane Taubira" in intentions.keys():
                # this return assumes there is only one Taubira hypothesis
                return pd.json_normalize(hypothesis)
        return pd.json_normalize(hypothesis)

    @staticmethod
    def _format_2022_polls(new_polls: pd.DataFrame) -> pd.DataFrame:

        # different renames
        to_rename = new_polls.filter(regex="intentions\.").columns
        new_names = (
            new_polls.filter(regex="intentions\.")
            .columns.str.split(".", expand=True)
            .droplevel(0)
        )
        AFFILIATIONS = {
            "Jean-Luc Mélenchon": "nbfarleft",
            "Anne Hidalgo": "nbleft",
            "Yannick Jadot": "nbgreen",
            "Emmanuel Macron": "nbcenter",
            "Valérie Pécresse": "nbright",
            "Marine Le Pen": "nbfarright",
            "Éric Zemmour": "nbsouverainistes",
        }

        new_polls = (
            new_polls.rename(
                columns=(
                    {"institut": "sondage", "intentions_exprimees": "samplesize"}
                    | dict(zip(to_rename, new_names))
                )
            )
            .rename(columns=AFFILIATIONS)
            .replace({"Harris interactive": "Harris", "Opinionway": "OpinionWay"})
        )

        # compute median date
        new_polls["date"] = pd.to_datetime(
            np.median(
                new_polls[["date_debut", "date_fin"]].values.astype(np.int64), axis=1
            )
        )
        new_polls["date"] = new_polls["date"].dt.date
        new_polls["dateelection"] = pd.to_datetime("2022-04-10")
        new_polls = new_polls.drop(["date_debut", "date_fin"], axis=1)

        # aggregate other parties:
        core_cols = ["sondage", "date", "dateelection", "samplesize"] + list(
            AFFILIATIONS.values()
        )
        rest = new_polls[new_polls.columns.difference(core_cols)]
        new_polls["nbother"] = rest.sum(axis=1)

        return new_polls.drop(rest.columns, axis=1)

    def _clean_polls(
        self,
        polls: pd.DataFrame,
        test_cutoff: pd.Timedelta = None,
    ) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:

        results_raw, results_mult, polls = self._format_polls(
            polls, self.political_families
        )
        (
            polls_train,
            polls_test,
        ) = self._train_split(polls, test_cutoff)

        return polls_train, polls_test, results_raw, results_mult

    def _format_polls(
        self, polls: pd.DataFrame, parties_complete: List[str]
    ) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        # start all elections on Jan 1st
        dfs = []
        for date in polls.dateelection.unique():
            date = pd.to_datetime(date)
            df = polls[(polls.dateelection == date) & (polls.date >= f"{date.year}-01")]
            df["countdown"] = dates_to_idx(df["date"], reference_date=date).astype(int)
            dfs.append(df)

        # compute "other" category
        polls = (
            pd.concat(dfs)
            .set_index(["dateelection", "date", "countdown", "sondage", "samplesize"])
            .rename(
                columns={
                    col: col.split("nb")[1] for col in polls if col.startswith("nb")
                }
            )[parties_complete[:-1]]
        )
        polls["other"] = 100 - polls.sum(1)
        np.testing.assert_allclose(polls.sum(1).values, 100)

        # isolate results
        polls = polls.reset_index()
        results_raw = polls[polls.sondage == "result"]
        polls = polls[polls.sondage != "result"].set_index(["date", "sondage"])

        # cast polls as multinomial obs
        polls = self.cast_as_multinomial(polls)

        # cast results as multinomial
        results_mult = self.results_as_multinomial(results_raw)

        return results_raw, results_mult, polls.reset_index()

    def results_as_multinomial(self, results_raw: pd.DataFrame) -> pd.DataFrame:
        # need number of people who voted
        raw_json = pd.read_json(
            "https://raw.githubusercontent.com/pollsposition/data/main/resultats/presidentielles"
            ".json",
        )
        raw_json = raw_json.loc["premier_tour"].to_dict()

        jsons = []
        for year, dateelection in zip(
            results_raw.dateelection.dt.year.unique(), results_raw.dateelection.unique()
        ):
            try:
                df = pd.json_normalize(raw_json[year])[["exprimes"]]
                df["dateelection"] = dateelection
                jsons.append(df)
            # 2022 results not available yet
            except KeyError:
                continue
        jsons = pd.concat(jsons)

        results_mult = (
            results_raw.join(jsons.set_index("dateelection"), on="dateelection")
            .drop("samplesize", axis="columns")
            .rename(columns={"exprimes": "samplesize"})
        )
        results_mult["samplesize"] = (
            results_mult["samplesize"] // 100
        )  # to prevent overflow in Multinomial

        return self.cast_as_multinomial(results_mult)

    def cast_as_multinomial(self, df: pd.DataFrame) -> pd.DataFrame:
        df[self.political_families] = (
            (df[self.political_families] / 100)
            .mul(df["samplesize"], axis=0)
            .round()
            .fillna(0)
            .astype(int)
        )
        df["samplesize"] = df[self.political_families].sum(1)

        return df

    @staticmethod
    def _train_split(
        polls: pd.DataFrame, test_cutoff: pd.Timedelta = None
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        last_election = polls.dateelection.unique()[-1]
        polls_train = polls[polls.dateelection != last_election]
        polls_test = polls[polls.dateelection == last_election]

        if test_cutoff:
            test_cutoff_ = last_election - test_cutoff
        else:
            test_cutoff_ = last_election - pd.Timedelta(2, "D")

        polls_train = pd.concat(
            [polls_train, polls_test[polls_test.date <= test_cutoff_]]
        )
        polls_test = polls_test[polls_test.date > test_cutoff_]

        return polls_train, polls_test

    def _load_predictors(self):
        self.unemployment_data = self._load_generic_predictor(
            "https://raw.githubusercontent.com/pollsposition/data/main/predicteurs"
            "/chomage_national_trim.csv",
            name="unemployment",
            freq="Q",
            skiprows=2,
        )
        self.polls_train, self.polls_test, self.results_mult = self._merge_with_data(
            self.unemployment_data, freq="Q"
        )
        return

    def _merge_with_data(
        self, predictor: pd.DataFrame, freq: str
    ) -> List[pd.DataFrame]:
        polls_train = self.polls_train.copy()
        polls_test = self.polls_test.copy()
        results_mult = self.results_mult.copy()
        dfs = []

        for data in [polls_train, polls_test, results_mult]:
            # add freq to data
            data.index = data["date"].dt.to_period(freq)
            # merge with data
            dfs.append(data.join(predictor).reset_index(drop=True))

        return dfs

    @staticmethod
    def _load_generic_predictor(
        file: str, name: str, freq: str, skiprows: int, sep: str = ";"
    ) -> pd.DataFrame:

        data = pd.read_csv(
            file,
            sep=sep,
            skiprows=skiprows,
        ).iloc[:, [0, 1]]
        data.columns = ["date", name]
        data = data.sort_values("date")

        # as timestamps variables:
        data.index = pd.period_range(
            start=data.date.iloc[0], periods=len(data), freq=freq
        )

        return data.drop("date", axis=1)

    def _standardize_continuous_predictors(self) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """Substract mean and divide by std to help with sampling and setting priors."""
        continuous_predictors = [
            "unemployment",
        ]
        self.continuous_predictors = (
            pd.concat(
                [
                    self.polls_train[["date"] + continuous_predictors],
                    self.results_mult[["date"] + continuous_predictors],
                ]
            )
            .set_index("date")
            .sort_index()
        )
        cont_preds_stdz = standardize(self.continuous_predictors)

        return (
            cont_preds_stdz.loc[self.unique_elections],
            cont_preds_stdz.loc[
                self.continuous_predictors.index.difference(self.unique_elections)
            ],
        )

    def build_model(
        self,
        polls: pd.DataFrame = None,
        continuous_predictors: pd.DataFrame = None,
    ) -> pm.Model:
        """Build and return a pymc3 model for the poll results and fundamental data.

        Parameters
        ----------
        polls
            Poll results from past and current elections. This only needs to be
            specified for out-of-sample predictions to run the model on another
            dataset than the training data.
        continuous_predictors
            Continuous predictors, or fundamentals. This only need to be
            specified for out-of-sample predictions to run the model on another
            dataset than the training data.

        Returns
        -------
        A PyMC model in the form of a pymc.Model() instance.

        """
        (
            self.pollster_id,
            self.countdown_id,
            self.election_id,
            self.coords,
        ) = self._build_coords(polls)

        with pm.Model(coords=self.coords) as model:

            data_containers, non_competing_parties = self._build_data_containers(
                polls, continuous_predictors
            )

            # --------------------------------------------------------
            #                   BASELINE COMPONENTS
            # --------------------------------------------------------

            # Baseline latent popularity for each political family. Shared
            # across elections.
            party_baseline_sd = pm.HalfNormal("party_baseline_sd", 0.5)
            party_baseline = ZeroSumNormal(
                "party_baseline", sigma=party_baseline_sd, dims="parties_complete"
            )

            # Election-specific deviation from baseline of the latent popularity
            # of each political family.
            lsd_baseline = pm.Normal("election_party_baseline_sd_baseline", sigma=0.5)
            lsd_party_effect = ZeroSumNormal(
                "election_party_baseline_sd_party_effect",
                sigma=0.5,
                dims="parties_complete",
            )
            election_party_baseline_sd = pm.Deterministic(
                "election_party_baseline_sd",
                aet.exp(lsd_baseline + lsd_party_effect),
                dims="parties_complete",
            )
            election_party_baseline = (
                ZeroSumNormal(  # as a GP over elections to account for order?
                    "election_party_baseline",
                    sigma=election_party_baseline_sd[None, :],
                    dims=("elections", "parties_complete"),
                    zerosum_axes=(0, 1),
                )
            )

            # --------------------------------------------------------
            #                        HOUSE EFFECTS
            # --------------------------------------------------------

            # Baseline for polls' bias towards the different political families.
            # These biases are shared by pollsters, i.e. they can be interpreted
            # as the market's bias.
            poll_bias = (
                ZeroSumNormal(  # equivalent to no ZeroSum on pollsters in house_effects
                    "poll_bias",
                    sigma=0.15,
                    dims="parties_complete",
                )
            )

            # Baseline for house effect (per political family)
            house_effects = ZeroSumNormal(
                "house_effects",
                sigma=0.15,
                dims=("pollsters", "parties_complete"),
                zerosum_axes=(0, 1),
            )

            # Election-specific house effect (per political family)
            house_election_effects_sd = pm.HalfNormal(
                "house_election_effects_sd",
                0.15,
                dims=("pollsters", "parties_complete"),
            )
            house_election_effects_raw = ZeroSumNormal(
                "house_election_effects_raw",
                dims=("pollsters", "parties_complete", "elections"),
                zerosum_axes=(0, 1, 2),
            )
            house_election_effects = pm.Deterministic(
                "house_election_effects",
                house_election_effects_sd[..., None] * house_election_effects_raw,
                dims=("pollsters", "parties_complete", "elections"),
            )

            # --------------------------------------------------------
            #                  FUNDAMENTAL COMPONENT
            #
            # It is commonly assumed that the results of the elections
            # are mostly determined by economic fundamentals, and
            # that the opinion "drifts" towards this result during the
            # campaign, so to speak.
            #
            # The coefficient below accounts for the effect of the
            # unemployment on the election result.
            # --------------------------------------------------------

            unemployment_effect = ZeroSumNormal(
                "unemployment_effect",
                sigma=0.15,
                dims="parties_complete",
            )

            # --------------------------------------------------------
            #               TIME-VARYING COMPONENT
            #
            # The latent popularity of political families varies over
            # the course of an election. We model this evolution with
            # gaussian processes.
            #
            # We currently use gaussian processes with 3 different
            # lengthscales to account for the typical timescales over which
            # opinion can change.
            #
            # The time evolution has two components: one that is common to all
            # elections (the baseline), and another one for each election,
            # which is a deviation from the common baseline.
            # --------------------------------------------------------

            # Build the gaussian process basis functions
            gp_basis_funcs, gp_basis_dim = make_gp_basis(
                time=self.coords["countdown"], gp_config=self.gp_config, key="parties"
            )

            # Baseline (shared across elections) for the time-varying component
            # of the latent popularity.
            # --------------------------------------------------------
            lsd_baseline = pm.Normal("lsd_baseline", sigma=0.3)
            lsd_party_effect = ZeroSumNormal(
                "lsd_party_effect_party_amplitude", sigma=0.2, dims="parties_complete"
            )
            party_time_weight = pm.Deterministic(
                "party_time_weight",
                aet.exp(lsd_baseline + lsd_party_effect),
                dims="parties_complete",
            )

            party_time_coefs_raw = ZeroSumNormal(
                "party_time_coefs_raw",
                sigma=1,
                dims=(gp_basis_dim, "parties_complete"),
                zerosum_axes=-1,
            )
            party_time_effect = pm.Deterministic(
                "party_time_effect",
                aet.tensordot(
                    gp_basis_funcs,
                    party_time_weight[None, ...] * party_time_coefs_raw,
                    axes=(1, 0),
                ),
                dims=("countdown", "parties_complete"),
            )

            # Election-specific time-varying component of the latent popularity
            # --------------------------------------------------------
            lsd_party_effect = ZeroSumNormal(
                "lsd_party_effect_election_party_amplitude",
                sigma=0.2,
                dims="parties_complete",
            )
            lsd_election_effect = ZeroSumNormal(
                "lsd_election_effect", sigma=0.2, dims="elections"
            )
            lsd_election_party_sd = pm.HalfNormal("lsd_election_party_sd", 0.2)
            lsd_election_party_raw = ZeroSumNormal(
                "lsd_election_party_raw",
                dims=("parties_complete", "elections"),
                zerosum_axes=(0, 1),
            )
            lsd_election_party_effect = pm.Deterministic(
                "lsd_election_party_effect",
                lsd_election_party_sd * lsd_election_party_raw,
                dims=("parties_complete", "elections"),
            )
            election_party_time_weight = pm.Deterministic(
                "election_party_time_weight",
                aet.exp(
                    lsd_party_effect[:, None]
                    + lsd_election_effect[None, :]
                    + lsd_election_party_effect
                ),
                dims=("parties_complete", "elections"),
            )

            election_party_time_coefs = ZeroSumNormal(
                "election_party_time_coefs",
                sigma=election_party_time_weight[None, ...],
                dims=(gp_basis_dim, "parties_complete", "elections"),
                zerosum_axes=(1, 2),
            )
            election_party_time_effect = pm.Deterministic(
                "election_party_time_effect",
                aet.tensordot(
                    gp_basis_funcs,
                    election_party_time_coefs,
                    axes=(1, 0),
                ),
                dims=("countdown", "parties_complete", "elections"),
            )

            # --------------------------------------------------------
            #                      POLL RESULTS
            #
            # In this section we use the variables defined before to
            # model the latent popularity of political families and how
            # this popularity translates into poll results.
            #
            # This part of the model is used to inform predictions about
            # the outcomes with the current state of polling; this is the
            # only place where poll results enter the model.
            # --------------------------------------------------------

            latent_mu = (
                party_baseline
                + election_party_baseline[data_containers["election_idx"]]
                + party_time_effect[data_containers["countdown_idx"]]
                + election_party_time_effect[
                    data_containers["countdown_idx"], :, data_containers["election_idx"]
                ]
                + aet.dot(
                    data_containers["stdz_unemp"][:, None], unemployment_effect[None, :]
                )
            )
            latent_mu = latent_mu + non_competing_parties["polls_additive"]
            pm.Deterministic(
                "latent_popularity",
                aet.nnet.softmax(latent_mu),
                dims=("observations", "parties_complete"),
            )
            noisy_mu = (
                latent_mu
                + poll_bias[None, :]  # let bias vary during election period?
                + house_effects[data_containers["pollster_idx"]]
                + house_election_effects[
                    data_containers["pollster_idx"], :, data_containers["election_idx"]
                ]
                * non_competing_parties["polls_multiplicative"]
            )

            noisy_popularity = pm.Deterministic(
                "noisy_popularity",
                aet.nnet.softmax(noisy_mu),
                dims=("observations", "parties_complete"),
            )

            # The concentration parameter of a Dirichlet-Multinomial distribution
            # can be interpreted as the effective number of trials.
            #
            # The mean (1000) is thus taken to be roughly the sample size of
            # polls, and the standard deviation accounts for the variation in
            # sample size.
            concentration_polls = pm.InverseGamma(
                "concentration_polls", mu=1000, sigma=200
            )

            pm.DirichletMultinomial(
                "N_approve",
                a=concentration_polls * noisy_popularity,
                n=data_containers["observed_N"],
                observed=data_containers["observed_polls"],
                dims=("observations", "parties_complete"),
            )

            # --------------------------------------------------------
            #                    ELECTION RESULTS
            #
            # In this section we use the variables defined before to model the
            # political families' latent popularity and how it translates into
            # results the day of the election.
            #
            # Results from previous elections enter the model here; poll
            # results enter indirectly via the latent variable and the above
            # regression.
            # --------------------------------------------------------

            latent_mu_t0 = (
                party_baseline
                + election_party_baseline
                + party_time_effect[0]
                + election_party_time_effect[0].T
                + aet.dot(
                    data_containers["election_unemp"][:, None],
                    unemployment_effect[None, :],
                )
            )
            latent_mu_t0 = latent_mu_t0 + non_competing_parties["results"]

            latent_pop_t0 = pm.Deterministic(
                "latent_pop_t0",
                aet.nnet.softmax(latent_mu_t0),
                dims=("elections", "parties_complete"),
            )

            # The concentration parameter of a Dirichlet-Multinomial distribution
            # can be interpreted as the effective number of trials.
            #
            # The mean (1000) is thus taken to be roughly the sample size of
            # polls, and the standard deviation accounts for the variation in
            # sample size.
            concentration_results = pm.InverseGamma(
                "concentration_results", mu=1000, sigma=200
            )
            pm.DirichletMultinomial(
                "R",
                a=concentration_results * latent_pop_t0[:-1],
                n=data_containers["results_N"],
                observed=data_containers["observed_results"],
                dims=("elections_observed", "parties_complete"),
            )

        return model

    def _build_coords(self, polls: pd.DataFrame = None):
        data = polls if polls is not None else self.polls_train

        COORDS = {
            "observations": data.index,
            "parties_complete": self.political_families,
        }
        pollster_id, COORDS["pollsters"] = data["sondage"].factorize(sort=True)
        countdown_id, COORDS["countdown"] = data["countdown"].values, np.arange(
            data["countdown"].max() + 1
        )
        election_id, COORDS["elections"] = data["dateelection"].factorize()
        COORDS["elections_observed"] = COORDS["elections"][:-1]

        return pollster_id, countdown_id, election_id, COORDS

    def _build_data_containers(
        self,
        polls: pd.DataFrame = None,
        campaign_predictors: pd.DataFrame = None,
    ) -> Tuple[Dict[str, pm.Data], Dict[str, np.ndarray]]:

        if polls is None:
            polls = self.polls_train
        if campaign_predictors is None:
            campaign_predictors = self.campaign_preds

        is_here = polls[self.political_families].astype(bool).astype(int)
        non_competing_parties = {
            "polls_multiplicative": is_here.values,
            "polls_additive": is_here.replace(to_replace=0, value=-10)
            .replace(to_replace=1, value=0)
            .values,
            "results": self.results_mult[self.political_families]
            .astype(bool)
            .astype(int)
            .replace(to_replace=0, value=-10)
            .replace(to_replace=1, value=0)
            .values,
        }

        data_containers = dict(
            election_idx=pm.Data("election_idx", self.election_id, dims="observations"),
            pollster_idx=pm.Data("pollster_idx", self.pollster_id, dims="observations"),
            countdown_idx=pm.Data(
                "countdown_idx", self.countdown_id, dims="observations"
            ),
            stdz_unemp=pm.Data(
                "stdz_unemp",
                campaign_predictors["unemployment"].to_numpy(),
                dims="observations",
            ),
            election_unemp=pm.Data(
                "election_unemp",
                self.results_preds["unemployment"].to_numpy(),
                dims="elections",
            ),
            observed_N=pm.Data(
                "observed_N",
                polls["samplesize"].to_numpy(),
                dims="observations",
            ),
            observed_polls=pm.Data(
                "observed_polls",
                polls[self.political_families].to_numpy(),
                dims=("observations", "parties_complete"),
            ),
            results_N=pm.Data(
                "results_N",
                self.results_oos["samplesize"].to_numpy(),
                dims="elections_observed",
            ),
            observed_results=pm.Data(
                "observed_results",
                self.results_oos[self.political_families].to_numpy(),
                dims=("elections_observed", "parties_complete"),
            ),
        )

        return data_containers, non_competing_parties

    def sample_all(
        self, *, model: pm.Model = None, var_names: List[str], **sampler_kwargs
    ) -> arviz.InferenceData:
        """
        Sample the model and return the trace.

        TODO: Add 3 distinct functions to sample from the prior predictive,
        posterior and posterior predictive distributions.

        Parameters
        ----------
        model : optional
            A model previously created using `self.build_model()`.
            Build a new model if None (default)
        var_names: List[str]
            Variables names passed to `pm.fast_sample_posterior_predictive`
        **sampler_kwargs : dict
            Additional arguments to `pm.sample`
        """
        if model is None:
            model = self.build_model()

        with model:
            prior_checks = pm.sample_prior_predictive()
            trace = pm.sample(return_inferencedata=False, **sampler_kwargs)
            post_checks = pm.fast_sample_posterior_predictive(
                trace, var_names=var_names
            )

        return arviz.from_pymc3(
            trace=trace,
            prior=prior_checks,
            posterior_predictive=post_checks,
            model=model,
        )

    def forecast_election(self, idata: arviz.InferenceData) -> arviz.InferenceData:
        """
        Generate out-of-sample predictions for ``election_to_predict`` specified in ``__init__``.

        Parameters
        ----------
        idata: arviz.InferenceData
            Posterior trace generated by ``self.sample_all`` on the training dataset.
            The dataset used for predictions is generated automatically: one observation for each
            of the days in ``self.coords["countdown"]``. The corresponding values of predictors are
            handled automatically.
        """
        new_dates, oos_data = self._generate_oos_data(idata)
        oos_data = self._join_with_continuous_predictors(oos_data)
        forecast_data_index = pd.DataFrame(
            data=0,  # just a placeholder
            index=pd.MultiIndex.from_frame(oos_data),
            columns=self.political_families,
        )
        forecast_data = forecast_data_index.reset_index()

        PREDICTION_COORDS = {"observations": new_dates}
        PREDICTION_DIMS = {
            "latent_popularity": ["observations", "parties_complete"],
            "noisy_popularity": ["observations", "parties_complete"],
            "N_approve": ["observations", "parties_complete"],
        }

        forecast_model = self.build_model(
            polls=forecast_data,
            continuous_predictors=forecast_data,
        )
        with forecast_model:
            ppc = pm.fast_sample_posterior_predictive(
                idata,
                var_names=[
                    "party_baseline",
                    "latent_popularity",
                    "noisy_popularity",
                    "N_approve",
                    "latent_pop_t0",
                    "R",
                ],
            )
            ppc = arviz.from_pymc3_predictions(
                ppc,
                idata_orig=idata,
                inplace=False,
                coords=PREDICTION_COORDS,
                dims=PREDICTION_DIMS,
            )

        return ppc

    def _generate_oos_data(
        self, idata: arviz.InferenceData
    ) -> Tuple[pd.Index, pd.DataFrame]:

        countdown = idata.posterior["countdown"]
        elections = idata.posterior["elections"]

        estimated_days = np.tile(countdown[::-1], reps=len(elections))
        N_estimated_days = len(estimated_days)

        new_dates = [
            pd.date_range(
                periods=max(countdown.data) + 1,
                end=date,
                freq="D",
            ).to_series()
            for date in elections.data
        ]
        new_dates = pd.concat(new_dates).index

        oos_data = pd.DataFrame.from_dict(
            {
                "countdown": estimated_days,
                "dateelection": np.repeat(
                    self.unique_elections, repeats=len(countdown)
                ),
                "sondage": np.random.choice(
                    self.unique_pollsters, size=N_estimated_days
                ),
                "samplesize": np.random.choice(
                    self.results_oos["samplesize"].values, size=N_estimated_days
                ),
            }
        )
        oos_data["date"] = new_dates

        return new_dates, oos_data.set_index("date")

    def _join_with_continuous_predictors(self, oos_data: pd.DataFrame) -> pd.DataFrame:
        oos_data["quarter"] = oos_data.index.to_period("Q")
        oos_data["month"] = oos_data.index.to_period("M")

        oos_data = oos_data.join(self.unemployment_data, on="quarter")
        # check no missing values
        np.testing.assert_allclose(0, oos_data.isna().any().mean())

        # stdz predictors based on observed values
        oos_data["unemployment"] = (
            oos_data["unemployment"] - self.continuous_predictors["unemployment"].mean()
        ) / self.continuous_predictors["unemployment"].std()

        return oos_data.reset_index()
