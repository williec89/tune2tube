#!/usr/bin/env python3
# coding=UTF8

# tune2tubeclass.py
#
# Copyright (C) 2014-2018 Michiel Sikma and contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
# or implied. See the License for the specific language governing
# permissions and limitations under the License.
#
# This script contains code from <https://developers.google.com/>.

import argparse
import subprocess
import sys
import re
import os
import http.client as httplib
import httplib2
import random
import time
import mutagen
import math

from datetime import datetime, timedelta, date
from apiclient.discovery import build
from apiclient.errors import HttpError
from apiclient.http import MediaFileUpload
from oauth2client.client import (flow_from_clientsecrets,
                                 AccessTokenRefreshError)
from oauth2client.file import Storage
from oauth2client.tools import argparser, run_flow
from . utils import bytes_to_human, error_exit
from . tunetags import TuneTags

script_path = os.path.abspath(__file__)

# Get the parent directory of the script's location
parent_directory = os.path.dirname(script_path)
project_path = os.path.dirname(parent_directory)

class Tune2Tube(object):
    def __init__(self):
        timestamp = time.time()
        current_date = time.gmtime(timestamp)

        string_date = time.strftime('%Y-%m-%d__%H:%M:%S', current_date)

        self.settings = {
            # ffmpeg is a dependency for this script. ffprobe should be
            # installed along with ffmpeg.
            'path_ffmpeg': 'ffmpeg',
            'path_ffprobe': 'ffprobe',
            # Temporary output filename.
            'path_output': project_pat+'/'+string_date+'.mp4',
            # Version number.
            't2t_version': '0.1',
            # Whether to display ffmpeg/ffprobe output.
            'verbose': False,
            # Whether to only generate the video file without uploading it.
            'generate_only': False,
            # Whether to forego the usage of stored oauth2 tokens.
            # If set to True, you will need to authenticate using your
            # browser each time you use the script.
            'no_stored_auth': False,
            # Default title to use in case the user's own title is
            # an empty string.
            'default_title': '(Empty title)',
            # Default variables to use for the dynamically generated title.
            'default_title_vars': 'artist,title',
            # Whether to use the dynamically generated title
            # from the file's metadata.
            'dynamic_title': True,
            'title': None,
            'title_vars': None
        }

        # Explicitly tell the underlying HTTP transport library not to retry,
        # since we are handling retry logic ourselves.
        httplib2.RETRIES = 1

        # Maximum number of times to retry before giving up.
        self.max_retries = 10

        # Always retry when these exceptions are raised.
        self.retriable_exceptions = (
            httplib2.HttpLib2Error, IOError, httplib.NotConnected,
            httplib.IncompleteRead, httplib.ImproperConnectionState,
            httplib.CannotSendRequest, httplib.CannotSendHeader,
            httplib.ResponseNotReady, httplib.BadStatusLine
        )

        # Always retry when an apiclient.errors.HttpError with one of these
        # status codes is raised.
        self.retriable_status_codes = [500, 502, 503, 504]

        # This OAuth 2.0 access scope allows an application to upload files to
        # the authenticated user's YouTube channel, but doesn't allow other
        # types of access.
        self.youtube_base = 'https://www.googleapis.com'
        self.youtube_upload_scope = self.youtube_base + '/auth/youtube.upload'
        self.youtube_api_service_name = 'youtube'
        self.youtube_api_version = 'v3'

        # We can set our uploaded video to one of these statuses.
        self.valid_privacy_statuses = ('public', 'private', 'unlisted')

        # This variable defines a message to display if
        # the client_secrets_file is missing.
        self.missing_client_secrets_message = '''
%s: Error: Please configure OAuth 2.0.

To make this script run you will need to populate the client_secrets.json file
found at:

   %s

with information from the Developers Console, which can be accessed
through <https://console.developers.google.com/>. See the README.md file
for more details.
'''

        # Set up our command line argument parser.
        # The argparser is initialized in oauth2client/tools.py. We're just
        # adding our own arguments to the ones already defined there.
        argparser.description = '''Generates a video from an image and audio \
file and uploads it to Youtube.'''
        argparser.epilog = '''A Youtube Data API client key is required to \
use this script, as well as ffmpeg. For help on setting up these \
dependencies, see this project\'s Github page \
<http://github.com/msikma/tune2tube/> or the included README.md file.'''
        argparser.add_help = True
        # Manually add a help argument,
        # as it is turned off in oauth2client/tools.py.
        argparser.add_argument(
            '--no_stored_auth',
            action='store_true',
            help='Forego using stored oauth2 tokens.'
        )
        argparser.add_argument(
            'audio_file',
            help='Audio file (MP3, OGG, FLAC, etc).'
        )
        argparser.add_argument(
            'image_file',
            help='Image file (PNG, JPG, etc).'
        )
        argparser.add_argument(
            '--output',
            help='''Save the output video (.MP4) to a file rather than \
uploading it to Youtube.'''
        )
        argparser.add_argument(
            '--cs_json',
            help='''Path to the client secrets json file \
(default: client_secrets.json).''',
            default=project_path+'/client_secrets.json'
        )
        argparser.add_argument(
            '--privacy',
            choices=self.valid_privacy_statuses,
            help='Privacy status of the video (default: unlisted).',
            default='public'
        )
        argparser.add_argument(
            '--category',
            default='10',
            help='''Numeric video category (see the Github wiki for a list; \
the default is 10, Music).'''
        )
        argparser.add_argument(
            '--keywords',
            help='Comma-separated list of video keywords/tags.',
            default='Anime Lofi,Relaxing Study Music,Chill Anime Vibes,Serene Beats,Anime Characters,Peaceful Lofi,Relaxation Music'
        )
        mxgroup = argparser.add_mutually_exclusive_group()
        mxgroup.add_argument(
            '--title',
            help='''Video title string (default: \'%s\'). If neither --title \
nor --title_vars is specified, --title_vars will be used with its default \
value, unless this would result in \
an empty title.''' % self.settings['default_title']
        )
        mxgroup.add_argument(
            '--title_vars',
            nargs='?',
            help='''Comma-separated list of metadata variables to use as \
the video title (default: %s).''' % self.settings['default_title_vars']
        )
        argparser.add_argument(
            '--title_sep',
            help='''Separator for the title variables (default: \' - \', \
yielding e.g. \'Artist - Title\'). Ignored if \
using --title_str.''',
            default=' - '
        )
        argparser.add_argument(
            '--description',
            nargs='?',
            help='Video description string (default: empty string).',
            default=''
        )
        argparser.add_argument(
            '--add_metadata',
            help='''Adds a list of audio file metadata to the \
description (default: True).''',
            default=True
        )
        argparser.add_argument(
            '-V',
            '--version',
            action='version',
            version='%(prog)s ' + self.settings['t2t_version'],
            help='Show version number and exit.'
        )
        mxgroup = argparser.add_mutually_exclusive_group()
        mxgroup.add_argument(
            '-v',
            '--verbose',
            action='store_true',
            help='Verbose mode (display ffmpeg/ffprobe output).'
        )
        mxgroup.add_argument(
            '-q',
            '--quiet',
            action='store_true',
            help='Quiet mode.'
        )
        argparser.add_argument(
            '-h',
            '--help',
            action='help',
            default=argparse.SUPPRESS,
            help='Show this help message and exit.'
        )

        self.tunetags = TuneTags()

    def get_authenticated_service(self, args):
        '''
        Get authenticated and cache the result.
        '''
        flow = flow_from_clientsecrets(
            self.settings['client_secrets_file'],
            scope=self.youtube_upload_scope,
            message=self.missing_client_secrets_message % (
                'tune2tubeclass.py',
                os.path.abspath(os.path.join(
                    os.path.dirname(__file__),
                    self.settings['client_secrets_file']
                ))
            )
        )

        storage = Storage('%s-oauth2.json' % project_path+'/tune2tubeclass.py')
        credentials = storage.get()
        if credentials is None or credentials.invalid \
           or self.settings['no_stored_auth']:
            credentials = run_flow(flow, storage, args)

        return build(
            self.youtube_api_service_name,
            self.youtube_api_version,
            http=credentials.authorize(httplib2.Http())
        )
    
    def get_random_particle():
        files = [
            'particlesfg2.mp4',
            'particlebokeh2.mp4',
            'particlefg.mp4',
        ]
        return random.choice(files)


    def get_random_title():
        titles = Tune2Tube.get_list_of_titles()
        return random.choice(titles)

    def get_list_of_titles():
        titles = []
        with open(project_path+'/titles.txt', 'r') as file:
            for line in file:
                titles.append(line.strip())  # .strip() removes any leading/trailing whitespace

        return titles
        
    def get_description():
        description = ''
        with open(project_path+'/description.txt', 'r') as file:
            description = file.read()

        return description

    def seconds_to_rounded_hours(seconds):
        hours = seconds / 3600
        rounded_hours = math.ceil(hours)
        return rounded_hours

    def initialize_upload(self, youtube, args, upfile, time):
        '''
        Begin a resumable video upload.
        '''
        tags = None

        if self.settings['keywords']:
            tags = self.settings['keywords'].split(',')

        # If we need to generate a dynamic title, do so now.
        if self.settings['dynamic_title']:
            title_vars = self.settings['title_vars'].split(',')
            items = [self.settings['metadata'][n] for n in title_vars
                     if n in self.settings['metadata']]
            title = self.settings['title_sep'].join(items)
        else:
            title = self.settings['title']

        if title == '':
            hours = Tune2Tube.seconds_to_rounded_hours(time)
            if (hours > 1):
                title = "[%s Hours] | " % hours
            else:
                title = "[%s Hour] | " % hours
            title += str(Tune2Tube.get_random_title())

        # Add the metadata tags to the description if needed.
        description = str(Tune2Tube.get_description())

        if self.settings['add_metadata']:
            if description != '':
                description += '\n'
            # Sort the list of metadata, so that items with linebreaks go last.
            metalist = [{
                key: self.settings['metadata'][key]
            } for key in self.settings['metadata']]
            metalist = sorted(metalist, key=lambda x: '\n'
                              in list(x.values())[0])
            for tag in metalist:
                for key in tag:
                    # Prevent pictures from being added to the description.
                    if 'APIC' in key:
                        continue
                    value = tag[key]
                    nice_key = self.tunetags.tag_lookup(key, True)
                    if '\n' in value:
                        description += '\n----\n%s: %s\n' % (nice_key, value)
                    else:
                        description += '\n%s: %s' % (nice_key, value)

        body = {
            'snippet': {
                'title': title,
                'description': description,
                'tags': tags,
                'categoryId': self.settings['category']
            },
            'status': {
                'privacyStatus': self.settings['privacy']
            }
        }

        # Call the API's videos.insert method to create and upload the video.
        insert_request = youtube.videos().insert(
            part=','.join(body.keys()),
            body=body,
            media_body=MediaFileUpload(upfile, chunksize=-1, resumable=True)
        )

        filesize = os.path.getsize(upfile)
        print('Uploading file... (filesize: %s)' % bytes_to_human(filesize))
        self.resumable_upload(insert_request)

    def resumable_upload(self, insert_request):
        '''
        This method implements an exponential backoff strategy to resume a
        failed upload.
        '''
        response = None
        error = None
        retry = 0
        while response is None:
            # print(str(response))
            try:
                status, response = insert_request.next_chunk()

                if 'id' in response:
                    print('''Video ID `%s' was successfully uploaded. \
Its visibility is set to `%s'.''' % (response['id'], self.settings['privacy']))
                    print('''URL of the newly uploaded video: \
<https://www.youtube.com/watch?v=%s>''' % response['id'])
                    print('''It may take some time for the video to \
finish processing; typically 1-10 minutes.''')
                else:
                    error_exit('''The upload failed with an unexpected \
response: %s''' % response)
            except HttpError as e:
                if e.resp.status in self.retriable_status_codes:
                    error = '''A retriable HTTP error %d occurred:\n%s''' % (
                        e.resp.status, e.content
                    )
                else:
                    raise
            except self.retriable_exceptions as e:
                error = 'A retriable error occurred: %s' % e

            if error != None:
                print(error)
                retry += 1
                if retry > self.max_retries:
                    error_exit('''Too many upload errors. No longer \
attempting to retry.''')
                max_sleep = 2 ** retry
                sleep_seconds = random.random() * max_sleep
                print('''Sleeping %f seconds and then \
retrying...''' % sleep_seconds)
                time.sleep(sleep_seconds)

    def generate_video(self, audio, image):
        '''
        Encodes a video file from our audio and image input files.
        '''
        # Check to see if our files exist at all.
        if not (os.path.exists(audio)):
            print(str(audio))
            error_exit('please specify a valid audio  file')

        if not (os.path.exists(image)):
            print(os.path.exists(image))
            print(str(image))
            error_exit('please specify a valid image file')

        in_image_ext = os.path.splitext(image)[1]
        in_audio_ext = os.path.splitext(audio)[1]

        # Check our MP3/OGG/FLAC/etc file and get its duration.
        probe_cmd = [self.settings['path_ffprobe'], audio]
        try:
            probe_out = subprocess.check_output(
                probe_cmd,
                stderr=subprocess.STDOUT
            )
            if self.settings['verbose']:
                print(probe_out)
        except:
            error_exit('''couldn't probe the audio file \
(ffprobe might not be available)''')

        # Try to extract some metadata from the file using Mutagen.
        try:
            metadata = mutagen.File(audio)
        except AttributeError:
            metadata = []

        # Save a human-readable version of the metadata in the object.
        # Keep the original Mutagen output around too.
        self.settings['metadata'] = {}
        self.settings['orig_metadata'] = metadata
        if metadata != None:
            for tag in metadata:
                item = metadata[tag]
                # We join the item in case it's still a list, as in the case
                # of Vorbis.
                if isinstance(item, (list, tuple)):
                    item = [n for n in item if isinstance(n, (unicode))]
                    if item == []: continue
                    item = ''.join(item)
                elif not isinstance(item, str):
                    continue
                self.settings['metadata'][self.tunetags.tag_lookup(tag)] = \
                    str(item)

        # Lift the actual track duration string out of the output.
        duration = re.findall(b'Duration: (.+?),'.decode('utf-8'), probe_out.decode('utf-8'))

        # If we get valid output, parse the duration and get a seconds value.
        # Otherwise, stop the script.
        if len(duration):
            duration = duration[0]
        else:
            error_exit('''couldn't parse ffprobe's output. Try again with \
-v (--verbose) to see what went wrong.''')

        # Turn the string into a datetime format.
        try:
            audio_info = datetime.strptime(duration, '%H:%M:%S.%f')
            delta = timedelta(
                hours=audio_info.hour,
                minutes=audio_info.minute,
                seconds=audio_info.second,
                microseconds=audio_info.microsecond
            )
        except ValueError:
            error_exit('''encountered an error trying to determine the \
duration of the audio file. It could be in an unrecognized format, or \
longer than 24 hours. (Duration: %s, exception: %s)''' % (
                duration, sys.exc_info()[0]
            ))

        print('Using image file `%s\', size: %s.' % (
            image,
            os.path.getsize(image)
        ))
        print('Using audio file `%s\', size: %s, duration: %s.' % (
            audio,
            os.path.getsize(audio),
            duration
        ))

        if self.settings['metadata'] == []:
            print("Couldn't extract audio file tags. Continuing.")
        else:
            print('Extracted %d tag(s) from the audio file.' % len(
                self.settings['metadata']
            ))

        print('Encoding video file...')
        dir_path = os.path.dirname(os.path.realpath(__file__))
        print(dir_path)

        # Now call ffmpeg and produce the video.
        ffmpeg_cmd = [
            self.settings['path_ffmpeg'],
            '-framerate', '60',
            '-hwaccel', 'cuda',
            '-hwaccel_output_format', 'cuda',
            '-stream_loop', '-1',
            '-loop', '1',
            '-i', image,
            '-stream_loop', '-1',
            '-i', project_path+'/_src/'+Tune2Tube.get_random_particle(),
            '-stream_loop', '-1',
        ]
        print(ffmpeg_cmd)

        # Add the audio file.
        if in_audio_ext == '.flac' or in_audio_ext == '.wav':
            # mp4 doesn't take flac very well, so we'll convert it.
            # also make sure we encode wav files, since mp4 files with PCM
            # audio sometimes don't decode well and don't get accepted.
            ffmpeg_cmd.extend([
                # one input file is the audio
                '-i', audio,
                # for compatibility with various builds, we'll use MP3
                '-c:a', 'libmp3lame',
                # high quality CBR is good enough
                '-b:a', '320k',
            ])
        else:
            ffmpeg_cmd.extend([
                '-i', audio,
            ])
        # Add the video encoding options.
        timetake = delta.total_seconds()
        ffmpeg_cmd.extend([
            # use x264 as the video encoder
            # '-filter_complex', '"[0:v]scale=3840:2160[img];[img][1:v]blend=all_mode=lighten:all_opacity=1[out];[2:a]amerge=inputs=1[aout]"',
            '-filter_complex', '[0:v]scale=1920:1080,format=gbrp[img1];[1:v]scale=1920:1080,format=gbrp[img2];[img1][img2]blend=all_mode=lighten:all_opacity=1[out];[2:a]amerge=inputs=1[aout]',
            '-map', '[out]',
            '-map', '[aout]',
            '-c:v', 'h264_nvenc',
            '-b:v', '8000000',
            # duration of the video
            '-t', str(delta.total_seconds()),
            # '-t', '5',
            # 4:4:4 chroma subsampling (best quality)
            # as fast as possible, at cost of filesize
            # (uploading likely costs less time)
            # lossless quality
            '-qp', '0',
            # output
            '-c:a', 'mp3',
            '-y',
            self.settings['path_output']
        ])

        try:
            print("FFmpeg command:", " ".join(ffmpeg_cmd))
            probe_out = subprocess.check_output(
                ffmpeg_cmd
            )
            probe_out_str = probe_out.decode('utf-8')
            print(probe_out_str)
            if self.settings['verbose']:
                print(probe_out)
        except:
            error_exit('''encountered an error trying to generate the video. \
Try again with -v (--verbose) to see what went wrong. \
(Exception: %s)''' % sys.exc_info()[0])

        print('Successfully generated the file `%s\'.'
              % self.settings['path_output'])

        return timetake

    def upload_tune(self, audio, image, args, video_ready=False):
        '''
        Uploads a video to Youtube.
        '''
        if not video_ready:
            timetake = self.generate_video(audio, image)

        if self.settings['generate_only']:
            print('Skipping Youtube upload.')
            exit()

        # Now upload the file to Youtube.
        print('Authenticating using the Youtube API...')
        try:
            youtube = self.get_authenticated_service(args)
        except httplib2.ServerNotFoundError as e:
            error_exit('%s.' % e)

        try:
            self.initialize_upload(youtube, args, self.settings['path_output'], timetake)
        except HttpError as e:
            print('An HTTP error %d occurred:\n%s' % (
                e.resp.status,
                e.content
            ))
        except AccessTokenRefreshError as e:
            print('''The stored access token seems to be invalid. Delete any \
-oauth2.json files that may exist and try again, or try again with the \
--no_stored_auth switch.''')

    def change_settings(self, overrides):
        self.settings.update(overrides)
