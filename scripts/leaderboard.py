import os
import time
import logging
import datetime
import argparse
import numpy as np
import subprocess as sp
from pathlib import Path
from multiprocessing import Pool
import mlflow

import __init_path__
import env


class Leaderboard:
    def __init__(self,
                 trainer_path,
                 dataset_type,
                 run_name,
                 bundle_size=1,
                 verbose=False
                 ):

        if not os.path.exists(trainer_path):
            raise RuntimeError(f'Could not find trainer script {trainer_path}')

        self.trainer_path = trainer_path
        self.dataset_type = dataset_type
        self.run_name = run_name
        self.bundle_size = bundle_size
        self.leader_boards = ['kitti_4/6', 'discoman_v10', 'tum']
        self.verbose = verbose
        # self.leader_boards = ['tum_debug', 'discoman_debug']

    def submit(self):

        if self.dataset_type == 'leaderboard':
            self.submit_on_all_datasets()
        else:
            self.submit_bundle(self.dataset_type)

    def submit_on_all_datasets(self):

        pool = Pool(len(self.leader_boards))
        for d_type in self.leader_boards:
            print(f'Submiting {d_type}')
            pool.apply_async(self.submit_bundle, (d_type, ))
        pool.close()
        pool.join()

    def submit_bundle(self, dataset_type):

        self.setup_logger(dataset_type)
        logger = logging.getLogger('leaderboard')

        logger.info(f'Dataset {dataset_type}. Started submitting jobs')

        started_jobs_id = set()
        for b in range(self.bundle_size):
            job_id = self.submit_job(dataset_type, b)
            started_jobs_id.add(job_id)

        logger.info(f'Dataset {dataset_type}. Started started_jobs_id {started_jobs_id}')
        self.wait_jobs(dataset_type, started_jobs_id)

        logger.info(f'Dataset {dataset_type}. Averaging metrics')
        try:
            self.average_metrics(dataset_type)
        except Exception as e:
            logger.info(e)

    def submit_job(self, dataset_type, bundle_id):
        logger = logging.getLogger('leaderboard')
        cmd = self.get_lsf_command(dataset_type, self.run_name + f'_b_{bundle_id}')
        logger.info(f'Running command: {cmd}')

        p = sp.Popen(cmd, shell=True, stdout=sp.PIPE)
        outs, errs = p.communicate(timeout=4)

        job_id = str(outs).split(' ')[1][1:-1]
        return job_id

    def get_lsf_command(self, dataset_type: str, run_name: str) -> str:

        if dataset_type == 'discoman_v10':
            dataset_root = env.DISCOMAN_V10_PATH
        elif dataset_type == 'discoman_debug':
            dataset_root = env.DISCOMAN_V10_PATH
        elif dataset_type == 'kitti_4/6':
            dataset_root = env.KITTI_PATH
        elif dataset_type == 'tum':
            dataset_root = env.TUM_PATH
        elif dataset_type == 'tum_debug':
            dataset_root = env.TUM_PATH
        else:
            raise RuntimeError('Unknown dataset_type')

        command = ['bsub',
                   f'-o {Path.home().joinpath("lsf").joinpath("%J").as_posix()}',
                   '-gpu "num=1:mode=exclusive_process"',
                   'python',
                   f'-m [airugpub02, airugpua06, airugpua09, airugpua10]'
                   f'{self.trainer_path}',
                   f'--dataset_root {dataset_root}',
                   f'--dataset_type {dataset_type}',
                   f'--run_name {run_name}',
                   ]
        return ' '.join(command)

    @staticmethod
    def wait_jobs(dataset_type, started_jobs_id):

        logger = logging.getLogger('leaderboard')
        finished = False
        while not finished:

            p = sp.Popen(['bjobs'], shell=True, stdout=sp.PIPE)
            outs, errs = p.communicate()
            outs = outs.decode('utf-8').split('\n')
            job_ids = {outs[i].split(' ')[0] for i in range(1, len(outs) - 1)}

            still_running_jobs = started_jobs_id.intersection(job_ids)
            logger.info(f'Dataset {dataset_type}. Jobs {still_running_jobs} are still running')

            if still_running_jobs:
                time.sleep(10)
            else:
                finished = True
                logger.info(f'Dataset {dataset_type}. All jobs has been finished')

    def setup_logger(self, dataset_type):

        logger = logging.getLogger('leaderboard')
        logger.setLevel(logging.DEBUG)

        dataset_type = dataset_type.replace('/', '_')
        fh = logging.FileHandler(os.path.join(env.PROJECT_PATH, f'log_leaderboard_{dataset_type}.txt'), mode='w+')
        fh.setLevel(logging.DEBUG)
        logger.addHandler(fh)

        if self.verbose:
            sh = logging.StreamHandler()
            sh.setLevel(logging.DEBUG)
            logger.addHandler(sh)

    def average_metrics(self, dataset_type):

        metrics, model_name = self.load_metrics(dataset_type)

        aggregated_metrics = self.aggregate_metrics(metrics)

        metrics_mean = {k + '_mean': np.mean(v) for k, v in aggregated_metrics.items() if 'test' in k}
        metrics_var = {k + '_var': np.var(v) for k, v in aggregated_metrics.items() if 'test' in k}

        mlflow.set_tracking_uri(env.TRACKING_URI)
        mlflow.set_experiment(dataset_type)

        with mlflow.start_run(run_name=(self.run_name + "_av")):
            mlflow.log_param('run_name', self.run_name + "_av")
            mlflow.log_param('starting_time', datetime.datetime.now().isoformat())

            mlflow.log_param('model.name', model_name)
            mlflow.log_param('num_of_runs_to_average', len(metrics))

            mlflow.log_metrics(metrics_mean)
            mlflow.log_metrics(metrics_var)

    def load_metrics(self, dataset_type):

        client = mlflow.tracking.MlflowClient(env.TRACKING_URI)
        exp = client.get_experiment_by_name(dataset_type)
        exp_id = exp.experiment_id

        metrics = list()
        model_name = None
        for run_info in client.list_run_infos(exp_id):
            base_run_name = client.get_run(run_info.run_id).data.params['run_name'].split('_')
            base_run_name = '_'.join(base_run_name[:-2])
            if self.run_name == base_run_name:
                metrics.append(client.get_run(run_info.run_id).data.metrics)
                if not model_name:
                    model_name = client.get_run(run_info.run_id).data.params.get('model.name', 'Unknown')

        return metrics, model_name

    @staticmethod
    def aggregate_metrics(metrics):
        aggregated_metrics = {k: [] for k in metrics[0].keys()}

        for metric in metrics:
            for k, v in metric.items():
                aggregated_metrics.get(k, list()).append(v)
        return aggregated_metrics


if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument('--trainer_path', type=str, required=True)
    parser.add_argument('--dataset_type', '-t', type=str, required=True,
                        help='You can find availible exp names in odometry.preprocessing.dataset_configs.py')

    parser.add_argument('--run_name', '-n', type=str, help='Name of the run. Must be unique and specific',
                        required=True)
    parser.add_argument('--bundle_size', '-b', type=int, help='Number runs in evaluate', required=True)

    parser.add_argument('--verbose', '-v', action='store_true', help='Print output to console', default=False)

    args = parser.parse_args()

    leaderboard = Leaderboard(trainer_path=args.trainer_path,
                              dataset_type=args.dataset_type,
                              run_name=args.run_name,
                              bundle_size=args.bundle_size,
                              verbose=args.verbose
                              )

    leaderboard.submit()