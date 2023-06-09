# !/usr/bin/env python
__author__ = "Danelle Cline"
__copyright__ = "Copyright 2022, MBARI"
__credits__ = ["MBARI"]
__license__ = "GPL"
__maintainer__ = "Duane Edgington"
__email__ = "duane at mbari.org"
__doc__ = '''

Main entry point for deepsea-ai

@author: __author__
@status: __status__
@license: __license__
'''

from datetime import datetime

import boto3
import click
import shutil
import os
from pathlib import Path
from urllib.parse import urlparse

from deepsea_ai.config.config import Config
from deepsea_ai.commands import upload_tag, process, train, bucket, monitor
from deepsea_ai.config import config as cfg
from deepsea_ai.config import setup
from deepsea_ai.database import api, queries
from deepsea_ai import logger
from deepsea_ai.logger import info, err, debug, warn, critical
from deepsea_ai import __version__
from deepsea_ai.logger.job_cache import JobCache

default_config = cfg.Config(quiet=True)
default_config_ini = cfg.default_config_ini


def init(log_prefix: str = "deepsea_ai", config: str = default_config_ini) -> Config:
    python_path = Path('logs')
    logger.create_logger_file(python_path, log_prefix)

    # get the AWS profile from the environment and use it for all AWS commands
    if 'AWS_DEFAULT_PROFILE' in os.environ or 'AWS_PROFILE' in os.environ:
        if 'AWS_DEFAULT_PROFILE' in os.environ:
            info(
                f'AWS_DEFAULT_PROFILE is set to {os.environ["AWS_DEFAULT_PROFILE"]} and will be used for all AWS commands')
            profile = os.environ['AWS_DEFAULT_PROFILE']
            boto3.setup_default_session(profile_name=profile)
        if 'AWS_PROFILE' in os.environ:
            info(f'AWS_PROFILE is set to {os.environ["AWS_PROFILE"]} and will be used for all AWS commands')
            profile = os.environ['AWS_PROFILE']
            boto3.setup_default_session(profile_name=profile)

    # initialize the config file either from the default or a custom location
    if config:
        custom_config = cfg.Config(config)
    else:
        custom_config = cfg.Config()

    # create a job cache for tracking jobs in the path the user is running in
    JobCache(Path(os.getcwd()))

    # set the database for the job cache if it is defined in the config
    if custom_config('database', 'gql'):
        JobCache().set_database(custom_config('database', 'gql'))
    return custom_config


user_name = default_config.get_username()

# example s3 buckets for help
example_input_process_s3 = f's3://{user_name}-video-in-dev'
example_output_process_s3 = f's3://{user_name}-tracks-out-dev'
example_input_train_s3 = f's3://{user_name}-training-dev'
example_output_train_s3 = f's3://{user_name}-model-checkpoints-dev'


@click.group(context_settings={'help_option_names': ['-h', '--help']})
@click.version_option(
    __version__,
    '-V', '--version',
    message=f'%(prog)s, version %(version)s'
)
def cli():
    """
    Process deep sea video in AWS from a command line.
    """
    pass


@cli.command(name="setup")
@click.option('--config', type=str, required=False,
              help=f'Path to config file to override defaults in {default_config_ini}')
@click.option('--mirror', is_flag=True, default=False, help='Mirror docker images from dockerhub into your Elastic '
                                                            'Container Registry (ECR)')
def setup_command(config, mirror):
    """
     Setup your AWS environment. Only need to run this once unless running in a new AWS account.
    """
    init(log_prefix="deepsea_ai_setup")
    custom_config = cfg.Config(config)
    account = custom_config.get_account()
    region = custom_config.get_region()
    image_cfg = ['yolov5_ecr', 'deepsort_ecr', 'strongsort_ecr']
    image_tags = [custom_config('aws', t) for t in image_cfg]
    if mirror:
        setup.mirror_docker_hub_images_to_ecr(ecr_client=boto3.client("ecr"), account_id=account,
                                              region=region, image_tags=image_tags)
    setup.create_role(account_id=account)
    setup.store_role(default_config)

    # override the default config file with the custom one
    if config:
        shutil.copy2(config, cfg.default_config_ini)
    else:
        info(f'Using default config file {cfg.default_config_ini}')


@cli.command(name="ecsprocess")
@click.option('--config', type=str, default=default_config_ini,
              help=f'Path to config file to override defaults in {default_config_ini}')
@click.option('--check', is_flag=True, default=False, help='Check if video has been processed and loaded before '
                                                           'sending off a job. Requires a deepsea-ai GraphQL endpoint')
@click.option('-u', '--upload', is_flag=True, default=False,
              help='Set option to upload local video files to S3 bucket')
@click.option('--clean', is_flag=True, default=True,
              help='Clean up unused artifact (e.g. video) from s3 after processing')
@click.option('-i', '--input', type=str, default="/Volumes/M3/mezzanine/DocRicketts/2022/02/1423/",
              help='Path to the folder with video files to upload. These can be either mp4 or mov files that '
                   'ffmpeg understands. This can also be a single video file.')
@click.option('-e', '--exclude', type=str, multiple=True,
              help='Exclude directory or file. Excludes any directory or file that contains the given string')
@click.option('--cluster', type=str, required=True,
              help='Name of the cluster to use to batch process.  This must correspond to an available Elastic '
                   'Container Service cluster.')
@click.option('--job', type=str, required=True,
              help='Name of the job, e.g. DiveV4361 benthic outline')
@click.option('--conf-thres', type=click.FLOAT, default=.01, help='Confidence threshold for the model')
@click.option('--iou-thres', type=click.FLOAT, default=.1, help='IOU threshold for the model')
@click.option('--dry-run', is_flag=True, default=False, help='Run the command without actually submitting the job.')
def batchprocess_command(config, check, upload, clean, cluster, job, input, exclude, conf_thres, iou_thres, dry_run):
    """
     (optional) upload, then batch process in an ECS cluster
    """
    custom_config = init(log_prefix="deepsea_ai_ecsprocess", config=config)
    database = None
    if check:
        warn('Checking if video has been processed and loaded before sending off a job.'
             ' Requires a deepsea-ai GraphQL endpoint')
        database = api.DeepSeaAIClient(custom_config('database', 'gql'))

    input_path = Path(input)
    resources = custom_config.get_resources(cluster)
    user_name = custom_config.get_username()
    videos = custom_config.check_videos(input_path, exclude)
    tags = custom_config.get_tags(f'Video uploaded from {input} by user {user_name} ')

    total_submitted = 0
    for v in videos:
        loaded = False
        if database:
            info(f'Checking if {v.name} has already been processed and loaded into the database...')
            # Check if the video has already been loaded by looking it up by the media name per this job name
            medias = database.execute(queries.GET_MEDIA_IN_JOB,
                                      processing_job_name=f"{resources['PROCESSOR']}-{job}",
                                      media_name=v.name)

            # Found a media in the job as keyed by the processing name, so assume that this was already processed
            if len(medias['data']['mediaInJob']) > 0:
                info(f'Video {v.name} has already been processed and loaded...skipping')
                loaded = True

        if upload and not loaded:
            if dry_run:
                info(f'Dry run: Uploading {v.name} to S3 bucket {resources["VIDEO_BUCKET"]}')
            else:
                upload_tag.video_data([v], urlparse(f's3://{resources["VIDEO_BUCKET"]}'), tags)

        if not loaded:
            if dry_run:
                info(f'Dry run: Submitting {v.name} to {resources["PROCESSOR"]} for processing')
            else:
                process.batch_run(resources, v, job, user_name, clean, conf_thres, iou_thres)
            total_submitted += 1
        else:
            warn(f'Video {v.name} has already been processed and loaded...skipping')

    info(f'==== Submitted {total_submitted} videos to {resources["PROCESSOR"]} for processing =====')


@cli.command(name="process")
@click.option('--config', type=str, default=default_config_ini,
              help=f'Path to config file to override defaults in {default_config_ini}')
# this might be cleaner as an enum but click does not support that fully yet
@click.option('--tracker', default='deepsort',
              help='Tracking type: deepsort or strongsort')
@click.option('-i', '--input', type=str, required=True,
              help='Path to the folder with video files to upload. These can be either mp4 or mov files that '
                   'ffmpeg understands. This can also be a single video file.')
@click.option('-e', '--exclude', type=str, multiple=True,
              help='Exclude directory or file. Excludes any directory or file that contains the given string')
@click.option('--input-s3', type=str, required=True,
              help=f'Path to the s3 bucket with video files. These can be either mp4 or '
                   f'mov files that ffmpeg understands, e.g. s3://{example_input_process_s3}')
@click.option('--output-s3', type=str, required=True,
              help=f'Path to the s3 bucket to store the output, e.g. s3://{example_output_process_s3}')
@click.option('-m', '--model-s3', type=str, default=default_config('aws', 'yolov5_model_s3'),
              help='S3 location of the trained model tar gz file - must contain a model.tar.gz file with a valid YOLOv5 '
                   'Pytorch model.')
@click.option('-j', '--job-description', type=str, help='The job description to use for the processing')
@click.option('-c', '--config-s3', type=str, help='S3 location of tracking algorithm config yaml file')
@click.option('--reid-model-url', type=click.STRING, help='Location of the reid model')
@click.option('--model-size', type=click.INT, default=640, help='Size of the model, e.g. 640 or 1280')
@click.option('--conf-thres', type=click.FLOAT, default=.01, help='Confidence threshold for the model')
@click.option('--iou-thres', type=click.FLOAT, default=.1, help='IOU threshold for the model')
@click.option('-s', '--save-vid', is_flag=True, default=False,
              help='Set option to output original video with detection boxes overlaid.')
@click.option('--instance-type', type=str, default='ml.g4dn.xlarge',
              help='AWS instance type, e.g. ml.g4dn.xlarge, ml.c5.xlarge')
def process_command(config, tracker, input, exclude, input_s3, output_s3, model_s3, config_s3, model_size,
                    reid_model_url,
                    conf_thres, iou_thres, save_vid, job_description, instance_type):
    """
     upload video(s) then process with a model
    """
    custom_config = init(log_prefix="deepsea_ai_process", config=config)

    # get tags to apply to the resources for cost monitoring
    tags = custom_config.get_tags(job_description)

    input_path = Path(input)
    input_s3 = urlparse(input_s3.rstrip('/'))
    output_s3 = urlparse(output_s3.rstrip('/'))
    model_s3 = urlparse(model_s3.rstrip('/'))

    # create the buckets
    info(f'Creating buckets')

    if bucket.create(input_s3, tags) and bucket.create(output_s3, tags):

        videos = custom_config.check_videos(input_path, exclude)
        input_s3, size_gb = upload_tag.video_data(videos, input_s3, tags)

        # insert the datetime prefix to make a unique key for the output
        now = datetime.utcnow()
        prefix = now.strftime("%Y%m%dT%H%M%SZ")
        output_unique_s3 = urlparse(f"s3://{output_s3.netloc}/{output_s3.path.lstrip('/')}/{prefix}/")

        # estimate the volume size needed for the job; make it 2x the size of the input if saving the video
        if save_vid:
            volume_size_gb = int(2 * size_gb)
        else:
            volume_size_gb = int(1.25 * size_gb)

        process.script_processor_run(input_s3, output_unique_s3, model_s3, model_size, reid_model_url,
                                     volume_size_gb, instance_type, config_s3, save_vid, conf_thres,
                                     iou_thres, tracker, custom_config, tags)


@cli.command(name="upload")
@click.option('--config', type=str, default=default_config_ini,
              help=f'Path to config file to override defaults in {default_config_ini}')
@click.option('-i', '--input', type=click.Path(exists=True, file_okay=False, dir_okay=True), required=True,
              help='Path to the folder with video files to upload. These can be either mp4 or mov files that '
                   'ffmpeg understands.  This can also be a single video file.')
@click.option('--s3', type=str, help='S3 bucket to upload to, e.g. s3://902005-video-in-dev', required=True)
def upload_command(config, input, s3):
    """
    Upload videos
    """
    custom_config = init(log_prefix="deepsea_ai_upload", config=config)
    input_s3 = urlparse(s3.rstrip('/'))
    tags = custom_config.get_tags(f'Uploaded {input} to {s3}')
    bucket.create(input_s3, tags)
    videos = custom_config.check_videos(Path(input))
    upload_tag.video_data(videos, input_s3, tags)


@cli.command(name="train")
@click.option('--config', type=str, default=default_config_ini,
              help=f'Path to config file to override defaults in {default_config_ini}')
@click.option('--images', type=str, required=True,
              help='Path to compressed training images. Images should be put into a tar.gz file '
                   'that decompress into images/train images/val and optionally images/test. This compressed file should not include any archive artifacts,'
                   ' e.g. ._ files. For example, compress to avoid archives in Mac OSX with'
                   ' COPYFILE_DISABLE=1 tar -czf images.tar.gz images/.  ')
@click.option('--labels', type=str, required=True,
              help='Path to the compressed training labels in YOLO format. Labels should be put into a tar.gz file '
                   'that decompress into labels/. This compressed file should not include any archive artifacts,'
                   ' e.g. ._ files. For example, compress to avoid archives in Mac OSX with'
                   ' COPYFILE_DISABLE=1 tar -czf labels.tar.gz labels/.  ')
@click.option('--label-map', type=str, required=True,
              help='Path to a simple text file with the yolo names in the sorted order of the training label indexes')
@click.option('--input-s3', type=str, required=True,
              help=f'Path to the s3 bucket to save the training data, e.g. {example_input_train_s3} or {example_input_train_s3}')
@click.option('--output-s3', type=str, required=True,
              help=f'Path to the s3 bucket to store the output, e.g. {example_output_train_s3} or {example_output_train_s3}')
@click.option('--resume', type=bool, default=False, help="Resume training from previous run")
@click.option('--model', type=str, default='yolov5x', help=f"Model choice: {','.join(train.models)} ")
@click.option('--epochs', type=int, default=2, help='Number of epochs. Default 2.')
@click.option('--batch-size', type=int, default=2, help='Batch size. Default 2.')
@click.option('--instance-type', type=str, default='ml.p3.2xlarge',
              help='AWS instance type, e.g. ml.p3.2xlarge, ml.p3.8xlarge, ml.p3.16xlarge, ml.p4d.24xlarge')
def train_command(config, images, labels, label_map, input_s3, output_s3, resume, model, epochs, batch_size,
                  instance_type):
    """
     (optional) upload training data, then train a YOLOv5 model
    """
    custom_config = init(log_prefix="deepsea_ai_train", config=config)

    if instance_type == 'ml.p2.xlarge':
        critical(f'{instance_type} too small for model {model}. Choose ml.p3.2xlarge or better')
        raise Exception(f'{instance_type} too small for model {model}. Choose ml.p3.2xlarge or better')

    # get tags to apply to the resources for cost monitoring
    tags = custom_config.get_tags(f'Training {input_s3} with {model} batch {batch_size} instance {instance_type}')

    image_path = Path(images)
    label_path = Path(labels)
    name_path = Path(label_map)

    # strip off any training forward slashes
    input_s3 = urlparse(input_s3.rstrip('/'))
    output_s3 = urlparse(output_s3.rstrip('/'))

    data = [image_path, label_path, name_path]

    # create the buckets
    if bucket.create(input_s3, tags) and bucket.create(output_s3, tags):

        # upload and return the final bucket prefix to the training data and its total size
        input_training, size_gb = upload_tag.training_data(data, input_s3, tags, cfg.default_training_prefix)

        debug(f"Training data: {input_training} size: {size_gb} GB")

        # guess on how much volume is needed per each GB plus the size for the checkpoints
        volume_size_gb = int(2 * size_gb + 50)

        # insert the datetime prefix to make a unique key for the outputs
        now = datetime.utcnow()
        prefix = now.strftime("%Y%m%dT%H%M%SZ")

        if resume:  # resuming from previous bucket, so no need to set prefix
            ckpts_s3 = urlparse(f"s3://{output_s3.netloc}/{output_s3.path.lstrip('/')}")
        else:
            ckpts_s3 = urlparse(f"s3://{output_s3.netloc}/{output_s3.path.lstrip('/')}/{prefix}/checkpoints/")
        model_s3 = urlparse(f"s3://{output_s3.netloc}/{output_s3.path.lstrip('/')}/{prefix}/models/")

        # train
        train.yolov5(data, input_training, ckpts_s3, model_s3, epochs, batch_size, volume_size_gb, model, instance_type,
                     custom_config)


@cli.command(name="package")
@click.option('--s3', type=str, required=True,
              help=f"Bucket with the model checkpoints, e.g. s3://{example_output_train_s3}/20220821T005204Z"
              )
def package_command(s3):
    """
    Package a YOLOv5 model into a format that the deepsea-ai can use in its pipelines.
    This is done at the end of the train command automatically and stored in a model.tar.gz file.
    This is added in case checkpoints were generated outside the deepsea-ai-traing command, e.g. SageMaker Studio. Colab
    """
    init(log_prefix="deepsea_ai_package")
    train.package(urlparse(s3.rstrip('/')))


@cli.command(name="split")
@click.option('-i', '--input', type=str, required=True,
              help='Path to the root folder with images and labels, organized into labels/ and images/ folders files to split')
@click.option('-o', '--output', type=str, required=True,
              help='Path to the root folder to save the split, compressed files. If it does not exist, it will be created.')
def split_command(input: str, output: str):
    """
    Split data into train/val/test sets randomly per the following percentages 85%/10%/5%
    """
    init(log_prefix="deepsea_ai_split")
    input_path = Path(input)
    output_path = Path(output)

    if not output_path.exists():
        output_path.mkdir(parents=True)

    paths = [input_path / 'labels', input_path / 'images']

    exists = [not p.exists() for p in paths]
    if any(exists):
        err(f'Error: one or more {paths} missing')
        return

    train.split(Path(input), Path(output))


@cli.command(name="monitor")
@click.option('--cluster', type=str, required=True,
              help='Name of the cluster to query.  This must correspond to an available Elastic '
                   'Container Service cluster.')
@click.option('--config', type=str, required=False,
              help=f'Path to config file to override defaults in {default_config_ini}')
@click.option('--update-period', type=int, default=10, help='Update period to monitor a job; default is every 60 '
                                                            'seconds. Ignored if --job is not specified.Generates a '
                                                            'new report file in the reports/ folder')
@click.option('--job', type=str, required=True, multiple=True,
              help='Name of the job, e.g. DiveV4361 benthic outline')
def monitor_command(cluster: str, config, job: str, update_period: int):
    """
    Print monitoring information for the cluster
    """
    custom_config = init(log_prefix="deepsea_ai_monitor", config=config)
    resources = custom_config.get_resources(cluster)
    if resources:
        info(f'Monitoring job status of cluster {cluster}')
        m = monitor.Monitor(job, resources, update_period)
        m.start()
        m.join()
    else:
        warn(f'No resources found for cluster {cluster}. Try another cluster with --cluster <cluster_name>')


if __name__ == '__main__':
    try:
        start = datetime.utcnow()
        cli()
        end = datetime.utcnow()
        info(f'Done. Elapsed time: {end - start} seconds')
    except Exception as e:
        err(f'Exiting. Error: {e}')
        exit(-1)
