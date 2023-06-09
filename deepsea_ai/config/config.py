# !/usr/bin/env python
__author__ = "Danelle Cline, Duane Edgington"
__copyright__ = "Copyright 2023, MBARI"
__credits__ = ["MBARI"]
__license__ = "GPL"
__maintainer__ = "Duane Edgington"
__email__ = "duane at mbari.org"
__doc__ = '''

Configuration helper to setup defaults and fetch cloud configuration

@author: __author__
@status: __status__
@license: __license__
'''

import string

import boto3
from configparser import ConfigParser
import datetime as dt
import os
from botocore.exceptions import ClientError
from pathlib import Path
from typing import List
from deepsea_ai.logger import err, info, debug, warn, critical, exception

default_training_prefix = 'training'
default_config_ini = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.ini')

class Config:

    def __init__(self, path: str = None, quiet: bool = False):
        """
        Read the .ini file and parse it
        """
        self.parser = ConfigParser()
        if path:
            self.path = path
        else:
            self.path = default_config_ini

        if not os.path.isfile(self.path):
            raise Exception(f'Bad path to {self.path}. Is your {self.path} missing?')

        self.parser.read(self.path)
        lines = open(self.path).readlines()
        if not quiet:
            info(f"=============== Config file {self.path} =================")
            for l in lines:
                info(l.strip())

            if not path:
                info(f"============ You can override these settings by creating a customconfig.ini file and pass that "
                     f"in with --config=customconfig.ini =====")

    def __call__(self, *args, **kwargs):
        assert len(args) == 2
        return self.parser.get(args[0], args[1])

    def save(self, *args, **kwargs):
        assert len(args) == 3
        self.parser.set(section=args[0], option=args[1], value=args[2])
        with open(self.path, 'w') as fp:
            self.parser.write(fp)

    def get_role(self) -> str:
        """
        Get the user role; first check the environment variable, then the config
        :return role ARN string
        """
        if 'SAGEMAKER_ROLE' in os.environ:
            return os.environ['SAGEMAKER_ROLE']

        sagemaker_arn = self.__call__('aws', 'sagemaker_arn')
        if not sagemaker_arn:
            raise Exception('Run deepsea-ai setup or set the SAGEMAKER_ROLE environment variable')
        return sagemaker_arn

    staticmethod
    def get_account(self) -> str:
        """
        Get the account number associated with this user
        :return:
        """
        account_number = boto3.client('sts').get_caller_identity()['Account']
        info(f'Found account {account_number}')
        return account_number

    staticmethod
    def get_region(self) -> str:
        """
        Get the region associated with this user
        :return:
        """
        session = boto3.session.Session()
        region = session.region_name
        info(f'Found region {region}')
        return region

    staticmethod
    def get_username(self) -> str:
        """
        Get the user name using IAM; if IAM is not configured, this will default to the root user which may be the case
        for a new AWS account
        :return:
        """
        try:
            sts = boto3.client('sts')
            response = sts.get_caller_identity()
            user_name = response['Arn'].split("/")[-1].split("@")[0]
        except ClientError as e:
            # The user_name may be specified in the Access Denied message...
            user_name = "Unknown"

        return user_name


    staticmethod
    def get_tags(self, description: str) -> dict:
        """
        Configure tag dictionary to associate with any AWS resource.
        This is useful for cost accounting and general reporting
        :param description: descriptive information about the use
        :return dictionary with tag key/value pairs
        """
        organization = self.__call__('tags', 'organization')
        project_number = self.__call__('tags', 'project_number')
        stage = self.__call__('tags', 'stage')
        application = self.__call__('tags', 'application')
        user_name = self.get_username()
        deletion_date = (dt.datetime.utcnow() + dt.timedelta(days=90)).strftime('%Y%m%dT%H%M%SZ')
        tag_dict = [{'Key': f'{organization}:project-number', 'Value': project_number},
                    {'Key': f'{organization}:owner', 'Value': user_name},
                    {'Key': f'{organization}:description', 'Value': description},
                    {'Key': f'{organization}:stage', 'Value': stage},
                    {'Key': f'{organization}:application', 'Value': application},
                    {'Key': f'{organization}:deletion-date', 'Value': deletion_date},
                    {'Key': f'{organization}:created-by', 'Value': user_name}]

        # check that the tags conform to the AWS tagging standard (no spaces, no special characters)
        # iterate over the tag dictionary and check the key and value
        for tag in tag_dict:
            info(f'Checking tag {tag}')
            #  The allowed characters across services are: letters (a-z, A-Z), numbers (0-9),
            #  and spaces representable in UTF-8, and the following characters: + - = . _ : / @.
            allowed_chars = ['+', '-', '=', '.', '_', ':', '/', '@']
            allowed_letters = list(string.ascii_letters)
            allowed_numbers = list(string.digits)
            allowed = allowed_chars + allowed_letters + allowed_numbers
            if not any(char in tag['Value'] for char in allowed):
                msg = f'Tag {tag} has a value with special characters. Check your config.ini file. ' \
                        f'Special characters are not allowed in AWS tags, e.g. dots, etc.'
                err(msg)
                raise (msg)

        return tag_dict

    staticmethod
    def get_resources(self, stack_name: str) -> dict:
        """
        Get resources relevant to the pipeline from the stack name; see deepsea-ai/cluster/stacks
        :param stack_name: name of the stack to query in the ECS cluster
        :return: dictionary with resource names
        """
        client_cf = boto3.client('cloudformation')
        client_ecs = boto3.client('ecs')

        try:
            stack_resources = client_cf.list_stack_resources(StackName=stack_name)
            resources = {'CLUSTER': stack_name}

            # fetch the PROCESSOR, etc. environment variables for the single task in the stack; the job is keyed uniquely to it
            key = ['PROCESSOR', 'TRACK_QUEUE', 'VIDEO_QUEUE', 'DEAD_QUEUE', 'TRACK_BUCKET', 'VIDEO_BUCKET']
            for r in stack_resources['StackResourceSummaries']:
                if 'AWS::ECS::TaskDefinition' in r['ResourceType']:
                    arn = r['PhysicalResourceId']
                    task_def = client_ecs.describe_task_definition(taskDefinition=arn)
                    environment = task_def['taskDefinition']['containerDefinitions'][0]['environment']
                    for e in environment:
                        for k in key:
                            if k in e['name']:
                                resources[k] = e['value']
                    break
                if 'AWS::AutoScaling::AutoScalingGroup' in r['ResourceType']:
                    resources['ASG'] = r['PhysicalResourceId']
            return resources
        except ClientError as ex:
            exception(ex)
            if 'AccessDenied' in ex.response['Error']['Code']:
                critical('Access denied; verify you are using the correct AWS credentials')
                raise Exception('Access denied')
            if 'ExpiredToken' in ex.response['Error']['Code']:
                critical('Token expired; you need to re-authenticate your AWS credentials')
                raise Exception('Token expired')
        return None

    staticmethod
    def check_videos(self, input_path: Path, exclude: tuple) -> List[Path]:
        """
         Check for videos with acceptable suffixes and return the Paths to them
        :param input_path: input path to search (non-recursively) or a single video file
        :param exclude: directory or files to exclude from the list of videos to process
        :return:
        """
        vid_formats = ['.mov', '.avi', '.mp4', '.mpg', '.mpeg', '.m4v', '.wmv', '.mkv']  # acceptable video suffixes

        # convert exclude tuple to list
        excludes = list(exclude)
        if len(excludes) > 0:
            info(f'Excluding any video file or directory that contains {excludes}')
        else:
            info(f'No video file exclusions specified')

        def search(x: Path):
            if excludes:
                found = [x.name.__contains__(e) for e in excludes]
                return (True not in found and x.suffix.lower() in vid_formats and '._' not in x.name)
            else:
                return (x.suffix.lower() in vid_formats and '._' not in x.name)

        # if the input path is a directory, search for videos
        videos = []
        if input_path.is_dir():
            videos = [x for x in input_path.glob("**/*") if search(x)]
        else:
            if search(input_path):
                videos = [input_path]
        num_videos = len(videos)
        info(f'Found {num_videos} videos to process')
        if num_videos == 0:
            err(f'No videos found in {input_path}')
        assert (num_videos > 0), "No videos to process"
        video_paths = [Path(x) for x in videos]
        return video_paths
