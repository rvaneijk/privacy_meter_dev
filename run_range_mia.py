"""This file is the main entry point for running the privacy auditing tool."""

import argparse
import math
import os
import pickle
import time

import numpy as np
import torch
import yaml

from audit import get_average_audit_results, audit_models, sample_auditing_dataset
from dataset.range_dataset import RangeDataset, RangeSampler
from get_signals import get_model_signals
from models.utils import load_models, train_models, split_dataset_for_training
from util import (
    check_configs,
    setup_log,
    initialize_seeds,
    create_directories,
    load_dataset,
)
from ramia_scores import trim_mia_scores

# Enable benchmark mode in cudnn to improve performance when input sizes are consistent
torch.backends.cudnn.benchmark = True


def main():
    print(20 * "-")
    print("Privacy Meter Tool!")
    print(20 * "-")

    # Parse arguments
    parser = argparse.ArgumentParser(description="Run privacy auditing tool.")
    parser.add_argument(
        "--cf",
        type=str,
        default="configs/cifar10.yaml",
        help="Path to the configuration YAML file.",
    )
    args = parser.parse_args()

    # Load configuration file
    with open(args.cf, "rb") as f:
        configs = yaml.load(f, Loader=yaml.Loader)

    # Validate configurations
    check_configs(configs)

    # Initialize seeds for reproducibility
    initialize_seeds(configs["run"]["random_seed"])

    # Create necessary directories
    log_dir = configs["run"]["log_dir"]
    directories = {
        "log_dir": log_dir,
        "report_dir": f"{log_dir}/report_ramia",
        "signal_dir": f"{log_dir}/signals",
        "data_dir": configs["data"]["data_dir"],
    }
    create_directories(directories)

    # Set up logger
    logger = setup_log(
        directories["report_dir"], "time_analysis", configs["run"]["time_log"]
    )

    start_time = time.time()

    # Load the dataset
    baseline_time = time.time()
    dataset = load_dataset(configs, directories["data_dir"], logger)
    logger.info("Loading dataset took %0.5f seconds", time.time() - baseline_time)

    # Define experiment parameters
    num_experiments = configs["run"]["num_experiments"]
    num_reference_models = configs["audit"]["num_ref_models"]
    num_model_pairs = max(math.ceil(num_experiments / 2.0), num_reference_models + 1)

    # Load or train models
    baseline_time = time.time()
    models_list, memberships = load_models(
        log_dir, dataset, num_model_pairs * 2, configs, logger
    )
    if models_list is None:
        # Split dataset for training two models per pair
        data_splits, memberships = split_dataset_for_training(
            len(dataset), num_model_pairs
        )
        models_list = train_models(
            log_dir, dataset, data_splits, memberships, configs, logger
        )
    logger.info(
        "Model loading/training took %0.1f seconds", time.time() - baseline_time
    )

    # TODO: abstract the range dataset creation
    if os.path.exists(
        f"{directories['data_dir']}/{configs["data"]["dataset"]}_range_auditing.pkl"
    ):
        with open(
            f"{directories['data_dir']}/{configs["data"]["dataset"]}_range_auditing.pkl",
            "rb",
        ) as file:
            dataset = pickle.load(file)
        logger.info(
            f"Load range data from {directories['data_dir']}/{configs["data"]["dataset"]}_range_auditing.pkl"
        )
    else:
        # Creating the range dataset
        logger.info("Creating range dataset.")
        dataset = RangeDataset(
            dataset,
            RangeSampler(
                range_fn=configs["ramia"]["range_function"],
                sample_size=configs["ramia"]["sample_size"],
                config=configs,
            ),
            configs,
        )

        with open(
            f"{directories['data_dir']}/{configs["data"]["dataset"]}_range_auditing.pkl",
            "wb",
        ) as f:
            pickle.dump(dataset, f)
        logger.info("Range dataset saved to disk.")

    # Subsampling the dataset for auditing
    auditing_dataset, auditing_membership = sample_auditing_dataset(
        configs, dataset, logger, memberships
    )

    # Generate signals (softmax outputs) for all models
    baseline_time = time.time()
    signals = get_model_signals(models_list, auditing_dataset, configs, logger)
    logger.info("Preparing signals took %0.5f seconds", time.time() - baseline_time)

    # Perform the privacy audit
    baseline_time = time.time()
    target_model_indices = list(range(num_experiments))
    # Expand the membership_list to match the shape of the auditing dataset
    mia_score_list, membership_list = audit_models_range(
        f"{directories['report_dir']}/exp",
        target_model_indices,
        signals,
        np.repeat(auditing_membership, configs["ramia"]["sample_size"], axis=1),
        num_reference_models,
        logger,
        configs,
    )

    if len(target_model_indices) > 1:
        logger.info(
            "Auditing privacy risk took %0.1f seconds", time.time() - baseline_time
        )

    # Get average audit results across all experiments
    if len(target_model_indices) > 1:
        get_average_audit_results(
            directories["report_dir"], mia_score_list, membership_list, logger
        )

    logger.info("Total runtime: %0.5f seconds", time.time() - start_time)


if __name__ == "__main__":
    main()
