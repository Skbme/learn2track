#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import division

import os
import sys

# Hack so you don't have to put the library containing this  script in the PYTHONPATH.
sys.path = [os.path.abspath(os.path.join(__file__, '..', '..'))] + sys.path

import numpy as np
import argparse
import csv
import re
import pickle
from collections import OrderedDict
# from texttable import Texttable

from os.path import join as pjoin

from smartlearner.utils import load_dict_from_json_file


DESCRIPTION = 'Gather experiments results and save them in a CSV file.'


def buildArgsParser():
    p = argparse.ArgumentParser(description=DESCRIPTION)
    p.add_argument('names', type=str, nargs='+', help='name/path of the experiments.')
    p.add_argument('--tractography-names', type=str, nargs='+', help='name of the tractography scores results. Default: wm', default=['wm'])
    p.add_argument('--out', default="results.csv", help='save table in a CSV file. Default: results.csv')
    p.add_argument('-v', '--verbose', action="store_true", help='verbose mode')
    return p


class Experiment(object):
    def __init__(self, experiment_path, tractography_name="wm"):
        self.description = tractography_name
        self.experiment_path = experiment_path
        self.name = os.path.basename(self.experiment_path)
        # self.logger_file = pjoin(self.experiment_path, "logger.pkl")
        self.results_file = pjoin(self.experiment_path, "results.json")
        self.tractometer_scores_file = pjoin(self.experiment_path, "tractometer", "scores", "{}.json".format(tractography_name))
        self.hyperparams_file = pjoin(self.experiment_path, "hyperparams.json")
        # self.model_hyperparams_file = pjoin(self.experiment_path, "GRU_Regression", "hyperparams.json")
        self.status_file = pjoin(self.experiment_path, "training", "status.json")
        self.early_stopping_file = pjoin(self.experiment_path, "training", "tasks", "early_stopping.json")

        self.results = {}
        if os.path.isfile(self.results_file):
            self.results = load_dict_from_json_file(self.results_file)

        self.hyperparams = load_dict_from_json_file(self.hyperparams_file)
        # self.model_hyperparams = load_dict_from_json_file(self.model_hyperparams_file)
        self.status = load_dict_from_json_file(self.status_file)

        self.tractometer_scores = {}
        if os.path.isfile(self.tractometer_scores_file):
            self.tractometer_scores = load_dict_from_json_file(self.tractometer_scores_file)
        elif os.path.isfile(self.tractometer_scores_file[:-5] + '.pkl'):
            self.tractometer_scores = pickle.load(open(self.tractometer_scores_file[:-5] + '.pkl', 'rb'))
        else:
            print("No tractometer results yet for: {}".format(self.tractometer_scores_file))

        self.early_stopping = {}
        if os.path.isfile(self.early_stopping_file):
            self.early_stopping = load_dict_from_json_file(self.early_stopping_file)


def list_of_dict_to_csv_file(csv_file, list_of_dicts):
    keys = list_of_dicts[0].keys()
    with open(csv_file, 'w') as output_file:
        dict_writer = csv.DictWriter(output_file, keys)
        dict_writer.writeheader()
        dict_writer.writerows(list_of_dicts)


def get_optimizer(e):
        if e.hyperparams.get("SGD") is not None:
            return "SGD"
        elif e.hyperparams.get("AdaGrad") is not None:
            return "AdaGrad"
        elif e.hyperparams.get("Adam") is not None:
            return "Adam"
        elif e.hyperparams.get("RMSProp") is not None:
            return "RMSProp"
        elif e.hyperparams.get("Adadelta") is not None:
            return "Adadelta"

        return ""


def extract_result_from_experiment(e):
    """e: `Experiment` object"""
    entry = OrderedDict()
    entry["Hidden Size(s)"] = "-".join(map(str, e.hyperparams.get("hidden_sizes", [])))
    entry["Regression"] = e.hyperparams.get("regression", "")
    entry["Classification"] = e.hyperparams.get("classification", "")
    entry["Weights Initialization"] = e.hyperparams.get("weights_initialization", "")
    entry["Look Ahead"] = e.hyperparams.get("lookahead", "")
    entry["Look Ahead eps"] = e.hyperparams.get("lookahead_eps", "")
    entry["Batch Size"] = e.hyperparams.get("batch_size", "")
    entry["Optimizer"] = get_optimizer(e)
    entry["Optimizer params"] = e.hyperparams.get(get_optimizer(e), "")
    entry["Nb. updates/epoch"] = e.hyperparams.get("nb_updates_per_epoch", "")
    entry["Noise sigma"] = e.hyperparams.get("noisy_streamlines_sigma", "")
    entry["Clip Gradient"] = e.hyperparams.get("clip_gradient", "")
    entry["Best Epoch"] = e.early_stopping.get("best_epoch", "")
    entry["Max Epoch"] = e.status.get("current_epoch", "")

    # Results
    # entry["Train L2 error"] = e.results["trainset"]["sequences_mean_loss_avg"]
    # entry["Valid L2 error"] = e.results["validset"]["sequences_mean_loss_avg"]
    # entry["Test L2 error"] = e.results["testset"]["sequences_mean_loss_avg"]

    # Tractometer results
    entry["VC"] = str(e.tractometer_scores.get("VC", ""))
    entry["IC"] = str(e.tractometer_scores.get("IC", ""))
    entry["NC"] = str(e.tractometer_scores.get("NC", ""))
    entry["VB"] = str(e.tractometer_scores.get("VB", ""))
    entry["IB"] = str(e.tractometer_scores.get("IB", ""))
    entry["count"] = str(e.tractometer_scores.get("total_streamlines_count", ""))
    entry["VCCR"] = ""
    if len(e.tractometer_scores) > 0:
        entry["VCCR"] = str(float(entry["VC"])/(float(entry["VC"])+float(entry["IC"])))

    overlap_per_bundle = e.tractometer_scores.get("overlap_per_bundle", {})
    overreach_per_bundle = e.tractometer_scores.get("overreach_per_bundle", {})
    entry["Avg. Overlap"] = str(np.mean(list(map(float, overlap_per_bundle.values()))))
    entry["Avg. Overreach"] = str(np.mean(list(map(float, overreach_per_bundle.values()))))
    entry["Std. Overlap"] = str(np.std(list(map(float, overlap_per_bundle.values()))))
    entry["Std. Overreach"] = str(np.std(list(map(float, overreach_per_bundle.values()))))

    streamlines_per_bundle = e.tractometer_scores.get("streamlines_per_bundle", {})
    entry['CA'] = str(streamlines_per_bundle.get("CA", ""))
    entry['CC'] = str(streamlines_per_bundle.get("CC", ""))
    entry['CP'] = str(streamlines_per_bundle.get("CP", ""))
    entry['CST_left'] = str(streamlines_per_bundle.get("CST_left", ""))
    entry['CST_right'] = str(streamlines_per_bundle.get("CST_right", ""))
    entry['Cingulum_left'] = str(streamlines_per_bundle.get("Cingulum_left", ""))
    entry['Cingulum_right'] = str(streamlines_per_bundle.get("Cingulum_right", ""))
    entry['FPT_left'] = str(streamlines_per_bundle.get("FPT_left", ""))
    entry['FPT_right'] = str(streamlines_per_bundle.get("FPT_right", ""))
    entry['Fornix'] = str(streamlines_per_bundle.get("Fornix", ""))
    entry['ICP_left'] = str(streamlines_per_bundle.get("ICP_left", ""))
    entry['ICP_right'] = str(streamlines_per_bundle.get("ICP_right", ""))
    entry['ILF_left'] = str(streamlines_per_bundle.get("ILF_left", ""))
    entry['ILF_right'] = str(streamlines_per_bundle.get("ILF_right", ""))
    entry['MCP'] = str(streamlines_per_bundle.get("MCP", ""))
    entry['OR_left'] = str(streamlines_per_bundle.get("OR_left", ""))
    entry['OR_right'] = str(streamlines_per_bundle.get("OR_right", ""))
    entry['POPT_left'] = str(streamlines_per_bundle.get("POPT_left", ""))
    entry['POPT_right'] = str(streamlines_per_bundle.get("POPT_right", ""))
    entry['SCP_left'] = str(streamlines_per_bundle.get("SCP_left", ""))
    entry['SCP_right'] = str(streamlines_per_bundle.get("SCP_right", ""))
    entry['SLF_left'] = str(streamlines_per_bundle.get("SLF_left", ""))
    entry['SLF_right'] = str(streamlines_per_bundle.get("SLF_right", ""))
    entry['UF_left'] = str(streamlines_per_bundle.get("UF_left", ""))
    entry['UF_right'] = str(streamlines_per_bundle.get("UF_right", ""))

    # Other results
    # entry["Train L2 error std"] = e.results["trainset"]["sequences_mean_loss_stderr"]
    # entry["Valid L2 error std"] = e.results["validset"]["sequences_mean_loss_stderr"]
    # entry["Test L2 error std"] = e.results["testset"]["sequences_mean_loss_stderr"]
    # entry["Train L2 error (per timestep)"] = e.results["trainset"]["timesteps_loss_avg"]
    # entry["Valid L2 error (per timestep)"] = e.results["validset"]["timesteps_loss_avg"]
    # entry["Test L2 error (per timestep)"] = e.results["testset"]["timesteps_loss_avg"]
    # entry["Train L2 error (per timestep) std"] = e.results["trainset"]["timesteps_loss_std"]
    # entry["Valid L2 error (per timestep) std"] = e.results["validset"]["timesteps_loss_std"]
    # entry["Test L2 error (per timestep) std"] = e.results["testset"]["timesteps_loss_std"]

    entry["Training Time"] = e.status.get("training_time", "")
    entry["Dataset"] = os.path.basename(e.hyperparams.get("dataset", ""))
    entry["Experiment"] = e.name
    entry["Description"] = e.description

    if "missing" in entry["Dataset"]:
        bundle_name = entry["Dataset"][:-4].split("_")[-1]
        missing_bundle_count = 0
        missing_bundle_overlap = []
        missing_bundle_overreach = []
        for k, v in streamlines_per_bundle.items():
            if k.startswith(bundle_name):
                missing_bundle_count += int(v)
                missing_bundle_overlap.append(overlap_per_bundle.get(k, 0))
                missing_bundle_overreach.append(overreach_per_bundle.get(k, 0))

        entry["Missing Bundle Count"] = str(missing_bundle_count)
        entry["Missing Bundle Overlap"] = str(np.mean(missing_bundle_overlap))
        entry["Missing Bundle Overreach"] = str(np.mean(missing_bundle_overreach))

    return entry


def main():
    parser = buildArgsParser()
    args = parser.parse_args()

    experiments_results = []

    for experiment_path in args.names:
        for tractography_name in args.tractography_names:
            try:
                experiment = Experiment(experiment_path, tractography_name)
                experiments_results.append(extract_result_from_experiment(experiment))
            except IOError as e:
                if args.verbose:
                    print(str(e))

                print("Skipping: '{}' for {}".format(experiment_path, tractography_name))

    list_of_dict_to_csv_file(args.out, experiments_results)


if __name__ == "__main__":
    main()
