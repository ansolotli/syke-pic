"""Join predictions and features to make final classification results"""

from pathlib import Path

import pandas as pd
from tqdm import tqdm

from sykepic.utils import logger
from sykepic.utils.ifcb import sample_to_datetime
from .prediction import prediction_dataframe, threshold_dictionary

DOLI_COILED_FACTOR_V2 = 7.056

NODU_COILED_FACTOR_BIGBV = 10.697
NODU_COILED_FACTOR_SMALLBV = 2.546
NODU_COILED_BV_THRESHOLD = 200000

log = logger.get_logger("class")


def main(args):
    probs = sorted(Path(args.probabilities).glob("**/*.csv"))
    out_file = Path(args.out)
    if out_file.suffix != ".csv":
        raise ValueError("Make sure output file ends with .csv")
    if out_file.is_file():
        if not (args.append or args.force):
            raise FileExistsError(f"{args.out} exists, --append or --force not used")
    if args.feat:
        feats = sorted(Path(args.feat).glob("**/*.csv"))
        df = class_df(
            probs,
            feats,
            thresholds_file=args.thresholds,
            divisions_file=args.divisions,
            summary_feature=args.value_column,
            progress_bar=True,
        )
    else:
        df = class_df_probs_only(probs, args.thresholds, progress_bar=True)
    df = swell_df(df)
    df_to_csv(df, out_file, args.append)


def class_df(
    probs,
    feats,
    thresholds_file,
    divisions_file=None,
    summary_feature="biomass_ugl",
    progress_bar=False,
):
    # Read probability thresholds
    thresholds = threshold_dictionary(thresholds_file)
    # Read feature divisions (optional)
    divisions = read_divisions(divisions_file) if divisions_file else None
    df_rows = []
    # Ensure probs and feats match
    if len(probs) != len(feats):
        iterator = (
            (p, f)
            for f in sorted(feats)
            for p in sorted(probs)
            if p.with_suffix("").stem == f.with_suffix("").stem
        )
    else:
        iterator = zip(sorted(probs), sorted(feats))
    # Add a tqdm progress bar optionally
    if progress_bar:
        iterator = tqdm(list(iterator), desc=f"Processing {len(feats)} samples")

    for prob_csv, feat_csv in iterator:
        # Check that CSVs match
        if prob_csv.with_suffix("").stem != feat_csv.with_suffix("").stem:
            raise ValueError(f"CSV mismatch: {prob_csv.name} & {feat_csv.name}")
        sample = prob_csv.with_suffix("").stem

        # Join prob, feat and classifications in one df
        try:
            sample_df = process_sample(prob_csv, feat_csv, thresholds, divisions)
        except KeyError:
            log.exception(prob_csv.with_suffix("").stem)
            continue
        # Select specific feature to summarize
        sample_column = sample_df[summary_feature]
        sample_column.name = sample
        df_rows.append(sample_column)

    # Create a collective dataframe for all samples
    # Make sure column names are deterministic
    classes = thresholds.keys()
    if divisions:
        division_names = names_of_divisions(divisions)
        classes = set(classes).union(division_names).difference(divisions.keys())
    classes = sorted(classes)
    classes.append("Total")
    df = pd.DataFrame(df_rows, columns=classes)
    df.index.name = "sample"
    df.fillna(0, inplace=True)
    return df


def class_df_probs_only(probs, thresholds_file, progress_bar=False):
    thresholds = threshold_dictionary(thresholds_file)
    classes = list(thresholds.keys()) + ["Total"]
    rows = []
    if progress_bar:
        iterator = tqdm(probs, desc=f"Processing {len(probs)} samples")
    else:
        iterator = probs
    for prob in iterator:
        sample = prob.with_suffix("").stem
        try:
            pdf = prediction_dataframe(prob, thresholds)
            gdf = pdf.groupby("prediction").sum()
        except KeyError:
            continue
        # frequency is based on the sum of True values in 'classified' column
        gdf.rename(columns={"classified": "abundance"}, inplace=True)
        gdf.index.name = "class"
        # Total frequency is the number of ROIs
        gdf.loc["Total"] = len(pdf)
        abun = gdf["abundance"]
        abun.name = sample
        rows.append(abun)
    df = pd.DataFrame(rows, columns=classes)
    df.index.name = "sample"
    df.fillna(0, inplace=True)
    return df.astype(int)


def swell_df(df):
    # Convert sample names to ISO 8601 timestamps (without microseconds)
    df.index = df.index.map(lambda x: sample_to_datetime(x, isoformat=True))
    df.index.name = "Time"
    # Sum Dolichospermum-Anabaenopsis variants together
    df["Dolichospermum-Anabaenopsis"] = df[
        ["Dolichospermum-Anabaenopsis", "Dolichospermum-Anabaenopsis_coiled"]
    ].sum(axis=1)
    df.drop("Dolichospermum-Anabaenopsis_coiled", axis=1, inplace=True)
    # Sum cyanobacteria
    cyano_sum = df[
        [
            "Aphanizomenon_flosaquae",
            "Dolichospermum-Anabaenopsis",
            "Nodularia_spumigena",
        ]
    ].sum(axis=1)
    df.insert(len(df.columns) - 1, "Filamentous cyanobacteria", cyano_sum)
    # Replace underscores with spaces in class names
    df.columns = df.columns.str.replace("_", " ")
    return df


def df_to_csv(df, out_file, append=False):
    append = append and Path(out_file).is_file()
    mode = "a" if append else "w"
    df.to_csv(out_file, mode=mode, header=not append)


def process_sample(
    prob_csv, feat_csv, thresholds, divisions=None, division_column="biovolume_px"
):
    # Join prediction and volume data by index (roi number)
    df = pd.concat(
        [
            prediction_dataframe(prob_csv, thresholds),
            pd.read_csv(feat_csv, index_col=0, comment="#"),
        ],
        axis=1,
    )
    df.index.name = "roi"

    
    NODU_EXC_COUNTER = 0
    #Apply conversion factor for coiled Nodularia
    for index, row in df.iterrows():
        if row['prediction'] == "Nodularia_spumigena-coiled":
            if row['biovolume_um3'] > 1000000:
                NODU_EXC_COUNTER += 1
                df.drop(index, inplace=True)
            elif row['biovolume_um3'] < NODU_COILED_BV_THRESHOLD:
                row['biovolume_um3'] /= NODU_COILED_FACTOR_SMALLBV
            else:
                row['biovolume_um3'] /= NODU_COILED_FACTOR_BIGBV


    # Record total feature results, before dropping unclassified rows
    total_biovolume_um3 = df["biovolume_um3"].sum()
    total_biomass_ugl = df["biomass_ugl"].sum()
    total_frequency = len(df)
    # Drop unclassified rows (below threshold)
    df = df[df["classified"]]
    # Make sure rows match (no empty biovolume values)
    assert not any(df.isna().any(axis=1))

    # Create intra-class divisions based on volume size
    if divisions:
        df = df.apply(divide_row, axis=1, args=((divisions, division_column)))

    # Group rows by prediction
    group = df.groupby("prediction", observed=False)

    # Calculate median biovolume and biomass for coiled Nodularia
    nodu_bv_median = df.loc[df["prediction"] == "Nodularia_spumigena-coiled"]["biovolume_um3"].median()
    nodu_bm_median = df.loc[df["prediction"] == "Nodularia_spumigena-coiled"]["biomass_ugl"].median()

    # Join biovolumes and frequencies
    gdf = group.sum()[["classified", "biovolume_um3", "biomass_ugl"]]
    gdf.rename(columns={"classified": "frequency"}, inplace=True)
    gdf.index.name = "class"

    # Add the median values times the number of dropped Nodularia images
    # to coiled Nodularia biovolume and biomass totals 
    try:
        gdf.loc["Nodularia_spumigena-coiled",
            "biovolume_um3"] += (nodu_bv_median*NODU_EXC_COUNTER)
        gdf.loc["Nodularia_spumigena-coiled",
            "biomass_ugl"] += (nodu_bm_median*NODU_EXC_COUNTER)
    except KeyError:
        pass

    # Sort by highest biomass
    gdf.sort_values("biomass_ugl", ascending=False, inplace=True)
    # Drop classes without any predictions
    gdf.drop(gdf[gdf["frequency"] <= 0].index, inplace=True)
    # Add totals to df
    gdf.loc["Total"] = [total_frequency, total_biovolume_um3, total_biomass_ugl]

    # Read feature extraction version from csv
    # with open(feat_csv) as fh:
    #     feat_version = int(fh.readline().strip().split("=")[1])
    # if feat_version == 2:
    # Apply conversion factor for "Dolichospermum-Anabaenopsis-coiled"
    try:
        gdf.loc["Dolichospermum-Anabaenopsis_coiled",
            "biovolume_um3"
        ] /= DOLI_COILED_FACTOR_V2
        gdf.loc["Dolichospermum-Anabaenopsis_coiled",
            "biomass_ugl"
        ] /= DOLI_COILED_FACTOR_V2
    except KeyError:
        pass
    return gdf


def read_divisions(division_file):
    divisions = {}
    with open(division_file) as fh:
        for line in fh:
            line = line.strip().split()
            key, *values = line
            divisions[key] = list(map(int, values))
    return divisions


def divide_row(row, divisions, column):
    row_name = row["prediction"]
    new_row_name = row_name
    if row_name in divisions:
        row_value = row[column]
        row_divisions = divisions[row_name]
        for i, division in enumerate(row_divisions):
            if row_value < division:
                if i == 0:
                    # prediction_under_9000
                    new_row_name = f"{row_name}_under_{division}"
                else:
                    # prediction_5000_9000
                    new_row_name = f"{row_name}_{row_divisions[i - 1]}_{division}"
            else:
                if i == len(row_divisions):
                    # prediction_9000_10000
                    new_row_name = f"{row_name}_{division}_{row_divisions[i + 1]}"
                else:
                    # prediction_over_9000
                    new_row_name = f"{row_name}_over_{division}"
    row["prediction"] = new_row_name
    return row


def names_of_divisions(divisions):
    new_names = []
    for key, values in divisions.items():
        values = sorted(values)
        new_names.append(f"{key}_under_{values[0]}")
        new_names.append(f"{key}_over_{values[-1]}")
        for i in range(len(values) - 1):
            new_names.append(f"{key}_{values[i]}_{values[i + 1]}")
    return new_names
