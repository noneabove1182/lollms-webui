######
# Project       : lollms-webui
# File          : api.py
# Author        : ParisNeo with the help of the community
# Supported by Nomic-AI
# license       : Apache 2.0
# Description   : 
# A simple api to communicate with lollms-webui and its models.
######
from flask import request
from datetime import datetime
from api.db import DiscussionsDB, Discussion
from api.helpers import compare_lists
from pathlib import Path
import importlib
from lollms.config import InstallOption
from lollms.types import MSG_TYPE, SENDER_TYPES
from lollms.personality import AIPersonality, PersonalityBuilder
from lollms.binding import LOLLMSConfig, BindingBuilder, LLMBinding, ModelBuilder
from lollms.paths import LollmsPaths
from lollms.helpers import ASCIIColors, trace_exception
from lollms.app import LollmsApplication
from lollms.utilities import File64BitsManager
import multiprocessing as mp
import threading
import time
import requests
from tqdm import tqdm 
import traceback
import sys
from lollms.terminal import MainMenu
import urllib
import gc
import ctypes
from functools import partial
import json

def terminate_thread(thread):
    if thread:
        if not thread.is_alive():
            ASCIIColors.yellow("Thread not alive")
            return

        thread_id = thread.ident
        exc = ctypes.py_object(SystemExit)
        res = ctypes.pythonapi.PyThreadState_SetAsyncExc(thread_id, exc)
        if res > 1:
            ctypes.pythonapi.PyThreadState_SetAsyncExc(thread_id, None)
            raise SystemError("Failed to terminate the thread.")
        else:
            ASCIIColors.yellow("Canceled successfully")

__author__ = "parisneo"
__github__ = "https://github.com/ParisNeo/lollms-webui"
__copyright__ = "Copyright 2023, "
__license__ = "Apache 2.0"



import subprocess
import pkg_resources


# ===========================================================
# Manage automatic install scripts

def is_package_installed(package_name):
    try:
        dist = pkg_resources.get_distribution(package_name)
        return True
    except pkg_resources.DistributionNotFound:
        return False


def install_package(package_name):
    try:
        # Check if the package is already installed
        __import__(package_name)
        print(f"{package_name} is already installed.")
    except ImportError:
        print(f"{package_name} is not installed. Installing...")
        
        # Install the package using pip
        subprocess.check_call(["pip", "install", package_name])
        
        print(f"{package_name} has been successfully installed.")


def parse_requirements_file(requirements_path):
    with open(requirements_path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                # Skip empty and commented lines
                continue
            package_name, _, version_specifier = line.partition('==')
            package_name, _, version_specifier = line.partition('>=')
            if is_package_installed(package_name):
                # The package is already installed
                print(f"{package_name} is already installed.")
            else:
                # The package is not installed, install it
                if version_specifier:
                    install_package(f"{package_name}{version_specifier}")
                else:
                    install_package(package_name)


# ===========================================================


class LoLLMsAPPI(LollmsApplication):
    def __init__(self, config:LOLLMSConfig, socketio, config_file_path:str, lollms_paths: LollmsPaths) -> None:

        super().__init__("Lollms_webui",config, lollms_paths, callback=self.process_chunk)
        self.is_ready = True
        
        
        self.socketio = socketio
        self.config_file_path = config_file_path
        self.cancel_gen = False

        # Keeping track of current discussion and message
        self._current_user_message_id = 0
        self._current_ai_message_id = 0
        self._message_id = 0

        self.db_path = config["db_path"]
        if Path(self.db_path).is_absolute():
            # Create database object
            self.db = DiscussionsDB(self.db_path)
        else:
            # Create database object
            self.db = DiscussionsDB(self.lollms_paths.personal_path/"databases"/self.db_path)

        # If the database is empty, populate it with tables
        ASCIIColors.info("Checking discussions database... ",end="")
        self.db.create_tables()
        self.db.add_missing_columns()
        ASCIIColors.success("ok")

        # This is used to keep track of messages 
        self.download_infos={}
        
        self.connections = {0:{
                "current_discussion":None,
                "generated_text":"",
                "cancel_generation": False,          
                "generation_thread": None,
                "processing":False,
                "schedule_for_deletion":False
            }
        }
        
        # =========================================================================================
        # Socket IO stuff    
        # =========================================================================================
        @socketio.on('connect')
        def connect():
            #Create a new connection information
            self.connections[request.sid] = {
                "current_discussion":None,
                "generated_text":"",
                "cancel_generation": False,          
                "generation_thread": None,
                "processing":False,
                "schedule_for_deletion":False
            }
            self.socketio.emit('connected', room=request.sid) 
            ASCIIColors.success(f'Client {request.sid} connected')

        @socketio.on('disconnect')
        def disconnect():
            try:
                self.socketio.emit('disconnected', room=request.sid) 
                if self.connections[request.sid]["processing"]:
                    self.connections[request.sid]["schedule_for_deletion"]=True
                else:
                    del self.connections[request.sid]
            except Exception as ex:
                pass
            
            ASCIIColors.error(f'Client {request.sid} disconnected')

        
        @socketio.on('cancel_install')
        def cancel_install(data):
            try:
                model_name = data["model_name"]
                binding_folder = data["binding_folder"]
                model_url = data["model_url"]
                signature = f"{model_name}_{binding_folder}_{model_url}"
                self.download_infos[signature]["cancel"]=True
                self.socketio.emit('canceled', {
                                                'status': True
                                                },
                                                room=request.sid 
                                    )            
            except Exception as ex:
                trace_exception(ex)
                self.socketio.emit('canceled', {
                                                'status': False,
                                                'error':str(ex)
                                                },
                                                room=request.sid 
                                    )            

        @socketio.on('install_model')
        def install_model(data):
            room_id = request.sid            
                     
            def install_model_():
                print("Install model triggered")
                model_path = data["path"]
                progress = 0
                installation_dir = self.lollms_paths.personal_models_path/self.config["binding_name"]
                filename = Path(model_path).name
                installation_path = installation_dir / filename
                print("Model install requested")
                print(f"Model path : {model_path}")

                model_name = filename
                binding_folder = self.config["binding_name"]
                model_url = model_path
                signature = f"{model_name}_{binding_folder}_{model_url}"
                self.download_infos[signature]={
                    "start_time":datetime.now(),
                    "total_size":self.binding.get_file_size(model_path),
                    "downloaded_size":0,
                    "progress":0,
                    "speed":0,
                    "cancel":False
                }
                
                if installation_path.exists():
                    print("Error: Model already exists")
                    socketio.emit('install_progress',{
                                                        'status': False,
                                                        'error': 'model already exists',
                                                        'model_name' : model_name,
                                                        'binding_folder' : binding_folder,
                                                        'model_url' : model_url,
                                                        'start_time': self.download_infos[signature]['start_time'].strftime("%Y-%m-%d %H:%M:%S"),
                                                        'total_size': self.download_infos[signature]['total_size'],
                                                        'downloaded_size': self.download_infos[signature]['downloaded_size'],
                                                        'progress': self.download_infos[signature]['progress'],
                                                        'speed': self.download_infos[signature]['speed'],
                                                    }, room=room_id
                                )
                
                socketio.emit('install_progress',{
                                                'status': True,
                                                'progress': progress,
                                                'model_name' : model_name,
                                                'binding_folder' : binding_folder,
                                                'model_url' : model_url,
                                                'start_time': self.download_infos[signature]['start_time'].strftime("%Y-%m-%d %H:%M:%S"),
                                                'total_size': self.download_infos[signature]['total_size'],
                                                'downloaded_size': self.download_infos[signature]['downloaded_size'],
                                                'progress': self.download_infos[signature]['progress'],
                                                'speed': self.download_infos[signature]['speed'],

                                                }, room=room_id)
                
                def callback(downloaded_size, total_size):
                    progress = (downloaded_size / total_size) * 100
                    now = datetime.now()
                    dt = (now - self.download_infos[signature]['start_time']).total_seconds()
                    speed = downloaded_size/dt
                    self.download_infos[signature]['downloaded_size'] = downloaded_size
                    self.download_infos[signature]['speed'] = speed

                    if progress - self.download_infos[signature]['progress']>2:
                        self.download_infos[signature]['progress'] = progress
                        socketio.emit('install_progress',{
                                                        'status': True,
                                                        'model_name' : model_name,
                                                        'binding_folder' : binding_folder,
                                                        'model_url' : model_url,
                                                        'start_time': self.download_infos[signature]['start_time'].strftime("%Y-%m-%d %H:%M:%S"),
                                                        'total_size': self.download_infos[signature]['total_size'],
                                                        'downloaded_size': self.download_infos[signature]['downloaded_size'],
                                                        'progress': self.download_infos[signature]['progress'],
                                                        'speed': self.download_infos[signature]['speed'],
                                                        }, room=room_id)
                    
                    if self.download_infos[signature]["cancel"]:
                        raise Exception("canceled")
                        
                    
                if hasattr(self.binding, "download_model"):
                    try:
                        self.binding.download_model(model_path, installation_path, callback)
                    except Exception as ex:
                        ASCIIColors.warning(str(ex))
                        socketio.emit('install_progress',{
                                    'status': False,
                                    'error': 'canceled',
                                    'model_name' : model_name,
                                    'binding_folder' : binding_folder,
                                    'model_url' : model_url,
                                    'start_time': self.download_infos[signature]['start_time'].strftime("%Y-%m-%d %H:%M:%S"),
                                    'total_size': self.download_infos[signature]['total_size'],
                                    'downloaded_size': self.download_infos[signature]['downloaded_size'],
                                    'progress': self.download_infos[signature]['progress'],
                                    'speed': self.download_infos[signature]['speed'],
                                }, room=room_id
                        )
                        del self.download_infos[signature]
                        try:
                            installation_path.unlink()
                        except Exception as ex:
                            ASCIIColors.error(f"Couldn't delete file. Please try to remove it manually.\n{installation_path}")
                        return

                else:
                    try:
                        self.download_file(model_path, installation_path, callback)
                    except Exception as ex:
                        ASCIIColors.warning(str(ex))
                        socketio.emit('install_progress',{
                                    'status': False,
                                    'error': 'canceled',
                                    'model_name' : model_name,
                                    'binding_folder' : binding_folder,
                                    'model_url' : model_url,
                                    'start_time': self.download_infos[signature]['start_time'].strftime("%Y-%m-%d %H:%M:%S"),
                                    'total_size': self.download_infos[signature]['total_size'],
                                    'downloaded_size': self.download_infos[signature]['downloaded_size'],
                                    'progress': self.download_infos[signature]['progress'],
                                    'speed': self.download_infos[signature]['speed'],
                                }, room=room_id
                        )
                        del self.download_infos[signature]
                        installation_path.unlink()
                        return                        
                socketio.emit('install_progress',{
                                                'status': True, 
                                                'error': '',
                                                'model_name' : model_name,
                                                'binding_folder' : binding_folder,
                                                'model_url' : model_url,
                                                'start_time': self.download_infos[signature]['start_time'].strftime("%Y-%m-%d %H:%M:%S"),
                                                'total_size': self.download_infos[signature]['total_size'],
                                                'downloaded_size': self.download_infos[signature]['downloaded_size'],
                                                'progress': 100,
                                                'speed': self.download_infos[signature]['speed'],
                                                }, room=room_id)
                del self.download_infos[signature]
                
            tpe = threading.Thread(target=install_model_, args=())
            tpe.start()

        @socketio.on('uninstall_model')
        def uninstall_model(data):
            model_path = data['path']
            installation_dir = self.lollms_paths.personal_models_path/self.config["binding_name"]
            filename = Path(model_path).name
            installation_path = installation_dir / filename
            
            model_name = filename
            binding_folder = self.config["binding_name"]

            if not installation_path.exists():
                socketio.emit('install_progress',{
                                                    'status': False,
                                                    'error': 'The model does not exist',
                                                    'model_name' : model_name,
                                                    'binding_folder' : binding_folder
                                                }, room=request.sid)
            try:
                installation_path.unlink()
                socketio.emit('install_progress',{
                                                    'status': True, 
                                                    'error': '',
                                                    'model_name' : model_name,
                                                    'binding_folder' : binding_folder
                                                }, room=request.sid)
            except Exception as ex:
                ASCIIColors.error(f"Couldn't delete {installation_path}, please delete it manually and restart the app")
                socketio.emit('install_progress',{
                                                    'status': False, 
                                                    'error': f"Couldn't delete {installation_path}, please delete it manually and restart the app",
                                                    'model_name' : model_name,
                                                    'binding_folder' : binding_folder
                                                }, room=request.sid)

        @socketio.on('new_discussion')
        def new_discussion(data):
            client_id = request.sid
            title = data["title"]
            self.connections[client_id]["current_discussion"] = self.db.create_discussion(title)
            # Get the current timestamp
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # Return a success response
            if self.connections[client_id]["current_discussion"] is None:
                self.connections[client_id]["current_discussion"] = self.db.load_last_discussion()
        
            if self.personality.welcome_message!="":
                message = self.connections[client_id]["current_discussion"].add_message(
                    message_type        = MSG_TYPE.MSG_TYPE_FULL.value if self.personality.include_welcome_message_in_disucssion else MSG_TYPE.MSG_TYPE_FULL_INVISIBLE_TO_AI.value,
                    sender_type         = SENDER_TYPES.SENDER_TYPES_AI.value,
                    sender              = self.personality.name,
                    content             = self.personality.welcome_message,
                    metadata            = None,
                    rank                = 0, 
                    parent_message_id   = -1, 
                    binding             = self.config.binding_name, 
                    model               = self.config.model_name,
                    personality         = self.config.personalities[self.config.active_personality_id], 
                    created_at=None, 
                    finished_generating_at=None
                )
 
                self.socketio.emit('discussion_created',
                            {'id':self.connections[client_id]["current_discussion"].discussion_id},
                            room=client_id
                )                        
            else:
                self.socketio.emit('discussion_created',
                            {'id':0},
                            room=client_id
                )                        

        @socketio.on('load_discussion')
        def load_discussion(data):
            client_id = request.sid
            ASCIIColors.yellow(f"Loading discussion for client {client_id}")
            if "id" in data:
                discussion_id = data["id"]
                self.connections[client_id]["current_discussion"] = Discussion(discussion_id, self.db)
            else:
                if self.connections[client_id]["current_discussion"] is not None:
                    discussion_id = self.connections[client_id]["current_discussion"].discussion_id
                    self.connections[client_id]["current_discussion"] = Discussion(discussion_id, self.db)
                else:
                    self.connections[client_id]["current_discussion"] = self.db.create_discussion()
            messages = self.connections[client_id]["current_discussion"].get_messages()

            self.socketio.emit('discussion',
                        [m.to_json() for m in messages],
                        room=client_id
            )


        @socketio.on('upload_file')
        def upload_file(data):
            file = data['file']
            filename = file.filename
            save_path = self.lollms_paths.personal_uploads_path/filename  # Specify the desired folder path

            try:
                if not self.personality.processor is None:
                    self.personality.processor.add_file(save_path, partial(self.process_chunk, client_id = request.sid))
                    file.save(save_path)
                    # File saved successfully
                    socketio.emit('progress', {'status':True, 'progress': 100})

                else:
                    # Personality doesn't support file sending
                    socketio.emit('progress', {'status':False, 'error': "Personality doesn't support file sending"})
            except Exception as e:
                # Error occurred while saving the file
                socketio.emit('progress', {'status':False, 'error': str(e)})
            
        @socketio.on('cancel_generation')
        def cancel_generation():
            client_id = request.sid
            self.cancel_gen = True
            #kill thread
            ASCIIColors.error(f'Client {request.sid} requested cancelling generation')
            terminate_thread(self.connections[client_id]['generation_thread'])
            ASCIIColors.error(f'Client {request.sid} canceled generation')
            self.cancel_gen = False

        @socketio.on('send_file')
        def send_file(data):
            client_id = request.sid
            self.connections[client_id]["generated_text"]       = ""
            self.connections[client_id]["cancel_generation"]    = False
            
            try:
                self.personality.setCallback(partial(self.process_chunk,client_id = client_id))
                ASCIIColors.info("Recovering file from front end")

                path:Path = self.lollms_paths.personal_uploads_path / self.personality.personality_folder_name
                path.mkdir(parents=True, exist_ok=True)
                file_path = path / data["filename"]
                File64BitsManager.b642file(data["fileData"],file_path)
                if self.personality.processor:
                    self.personality.processor.add_file(file_path, partial(self.process_chunk, client_id=client_id))
                    
                self.socketio.emit('file_received',
                        {
                            "status":True,
                        }, room=client_id
                )    
            except Exception as ex:
                ASCIIColors.error(ex)
                trace_exception(ex)
                self.socketio.emit('file_received',
                        {
                            "status":False,
                            "error":"Couldn't receive file: "+str(ex)
                        }, room=client_id
                )    
        
        @socketio.on('generate_msg')
        def generate_msg(data):
            client_id = request.sid
            self.connections[client_id]["generated_text"]=""
            self.connections[client_id]["cancel_generation"]=False
            
            if not self.model:
                self.notify("Model not selected. Please select a model", False, client_id)
                return
 
            if self.is_ready:
                if self.connections[client_id]["current_discussion"] is None:
                    if self.db.does_last_discussion_have_messages():
                        self.connections[client_id]["current_discussion"] = self.db.create_discussion()
                    else:
                        self.connections[client_id]["current_discussion"] = self.db.load_last_discussion()

                prompt = data["prompt"]
                ump = self.config.discussion_prompt_separator +self.config.user_name+": " if self.config.use_user_name_in_discussions else self.personality.user_message_prefix
                message = self.connections[client_id]["current_discussion"].add_message(
                    message_type    = MSG_TYPE.MSG_TYPE_FULL.value,
                    sender_type     = SENDER_TYPES.SENDER_TYPES_USER.value,
                    sender          = ump.replace(self.config.discussion_prompt_separator,"").replace(":",""),
                    content=prompt,
                    metadata=None,
                    parent_message_id=self.message_id
                )

                ASCIIColors.green("Starting message generation by "+self.personality.name)
                self.connections[client_id]['generation_thread'] = threading.Thread(target=self.start_message_generation, args=(message, message.id, client_id))
                self.connections[client_id]['generation_thread'].start()
                
                self.socketio.sleep(0.01)
                ASCIIColors.info("Started generation task")
                #tpe = threading.Thread(target=self.start_message_generation, args=(message, message_id, client_id))
                #tpe.start()
            else:
                self.notify("I am buzzy. Come back later.", False, client_id)

        @socketio.on('generate_msg_from')
        def generate_msg_from(data):
            client_id = request.sid
            if self.connections[client_id]["current_discussion"] is None:
                ASCIIColors.warning("Please select a discussion")
                self.notify("Please select a discussion first", False, client_id)
                return
            id_ = data['id']
            if id_==-1:
                message = self.connections[client_id]["current_discussion"].current_message
            else:
                message = self.connections[client_id]["current_discussion"].select_message(id_)
            if message is None:
                return            
            self.connections[client_id]['generation_thread'] = threading.Thread(target=self.start_message_generation, args=(message, message.id, client_id))
            self.connections[client_id]['generation_thread'].start()

        # generation status
        self.generating=False
        ASCIIColors.blue(f"Your personal data is stored here :",end="")
        ASCIIColors.green(f"{self.lollms_paths.personal_path}")


        @socketio.on('continue_generate_msg_from')
        def handle_connection(data):
            client_id = request.sid
            if self.connections[client_id]["current_discussion"] is None:
                ASCIIColors.yellow("Please select a discussion")
                self.notify("Please select a discussion", False, client_id)
                return
            id_ = data['id']
            if id_==-1:
                message = self.connections[client_id]["current_discussion"].current_message
            else:
                message = self.connections[client_id]["current_discussion"].select_message(id_)

            self.connections[client_id]['generation_thread'] = threading.Thread(target=self.start_message_generation, args=(message, message.id, client_id, True))
            self.connections[client_id]['generation_thread'].start()

        # generation status
        self.generating=False
        ASCIIColors.blue(f"Your personal data is stored here :",end="")
        ASCIIColors.green(f"{self.lollms_paths.personal_path}")


    def rebuild_personalities(self, reload_all=False):
        if reload_all:
            self.mounted_personalities=[]

        loaded = self.mounted_personalities
        loaded_names = [f"{p.language}/{p.category}/{p.personality_folder_name}" for p in loaded]
        mounted_personalities=[]
        ASCIIColors.success(f" ╔══════════════════════════════════════════════════╗ ")
        ASCIIColors.success(f" ║           Building mounted Personalities         ║ ")
        ASCIIColors.success(f" ╚══════════════════════════════════════════════════╝ ")
        to_remove=[]
        for i,personality in enumerate(self.config['personalities']):
            if i==self.config["active_personality_id"]:
                ASCIIColors.red("*", end="")
                ASCIIColors.green(f" {personality}")
            else:
                ASCIIColors.yellow(f" {personality}")
            if personality in loaded_names:
                mounted_personalities.append(loaded[loaded_names.index(personality)])
            else:
                personality_path = self.lollms_paths.personalities_zoo_path/f"{personality}"
                try:
                    personality = AIPersonality(personality_path,
                                                self.lollms_paths, 
                                                self.config,
                                                model=self.model,
                                                run_scripts=True)
                    mounted_personalities.append(personality)
                except Exception as ex:
                    ASCIIColors.error(f"Personality file not found or is corrupted ({personality_path}).\nReturned the following exception:{ex}\nPlease verify that the personality you have selected exists or select another personality. Some updates may lead to change in personality name or category, so check the personality selection in settings to be sure.")
                    ASCIIColors.info("Trying to force reinstall")
                    if self.config["debug"]:
                        print(ex)
                    try:
                        personality = AIPersonality(
                                                    personality_path, 
                                                    self.lollms_paths, 
                                                    self.config, 
                                                    self.model, 
                                                    run_scripts=True,
                                                    installation_option=InstallOption.FORCE_INSTALL)
                        mounted_personalities.append(personality)
                    except Exception as ex:
                        ASCIIColors.error(f"Couldn't load personality at {personality_path}")
                        trace_exception(ex)
                        ASCIIColors.info(f"Unmounting personality")
                        to_remove.append(i)
                        personality = AIPersonality(None,                                                    self.lollms_paths, 
                                                    self.config, 
                                                    self.model, 
                                                    run_scripts=True,
                                                    installation_option=InstallOption.FORCE_INSTALL)
                        mounted_personalities.append(personality)
                        ASCIIColors.info("Reverted to default personality")
        print(f'selected : {self.config["active_personality_id"]}')
        ASCIIColors.success(f" ╔══════════════════════════════════════════════════╗ ")
        ASCIIColors.success(f" ║                      Done                        ║ ")
        ASCIIColors.success(f" ╚══════════════════════════════════════════════════╝ ")
        # Sort the indices in descending order to ensure correct removal
        to_remove.sort(reverse=True)

        # Remove elements from the list based on the indices
        for index in to_remove:
            if 0 <= index < len(mounted_personalities):
                mounted_personalities.pop(index)
                self.config["personalities"].pop(index)
                ASCIIColors.info(f"removed personality {personality_path}")

        if self.config["active_personality_id"]>=len(self.config["personalities"]):
            self.config["active_personality_id"]=0
            
        return mounted_personalities
    # ================================== LOLLMSApp

    #properties
    @property
    def message_id(self):
        return self._message_id
    @message_id.setter
    def message_id(self, id):
        self._message_id=id

    @property
    def current_user_message_id(self):
        return self._current_user_message_id
    @current_user_message_id.setter
    def current_user_message_id(self, id):
        self._current_user_message_id=id
        self._message_id = id
    @property
    def current_ai_message_id(self):
        return self._current_ai_message_id
    @current_ai_message_id.setter
    def current_ai_message_id(self, id):
        self._current_ai_message_id=id
        self._message_id = id

    def download_file(self, url, installation_path, callback=None):
        """
        Downloads a file from a URL, reports the download progress using a callback function, and displays a progress bar.

        Args:
            url (str): The URL of the file to download.
            installation_path (str): The path where the file should be saved.
            callback (function, optional): A callback function to be called during the download
                with the progress percentage as an argument. Defaults to None.
        """
        try:
            response = requests.get(url, stream=True)

            # Get the file size from the response headers
            total_size = int(response.headers.get('content-length', 0))

            with open(installation_path, 'wb') as file:
                downloaded_size = 0
                with tqdm(total=total_size, unit='B', unit_scale=True, ncols=80) as progress_bar:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            file.write(chunk)
                            downloaded_size += len(chunk)
                            if callback is not None:
                                callback(downloaded_size, total_size)
                            progress_bar.update(len(chunk))

            if callback is not None:
                callback(total_size, total_size)

            print("File downloaded successfully")
        except Exception as e:
            print("Couldn't download file:", str(e))

    def condition_chatbot(self):
        if self.current_discussion is None:
            self.current_discussion = self.db.load_last_discussion()

        if self.personality.welcome_message!="":
            message_type = MSG_TYPE.MSG_TYPE_FULL.value# if self.personality.include_welcome_message_in_disucssion else MSG_TYPE.MSG_TYPE_FULL_INVISIBLE_TO_AI.value
            message_id = self.current_discussion.add_message(
                self.personality.name, self.personality.welcome_message,
                message_type,
                0,
                -1,
                binding= self.config["binding_name"],
                model = self.config["model_name"], 
                personality=self.config["personalities"][self.config["active_personality_id"]]
            )

            self.current_ai_message_id = message_id
        else:
            message_id = 0
        return message_id

    def prepare_reception(self, client_id):
        self.connections[client_id]["generated_text"] = ""
        self.nb_received_tokens = 0
    
    def create_new_discussion(self, title):
        self.current_discussion = self.db.create_discussion(title)
        # Get the current timestamp
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Chatbot conditionning
        self.condition_chatbot()
        return timestamp


    def prepare_query(self, client_id, message_id=-1, is_continue=False):
        messages = self.connections[client_id]["current_discussion"].get_messages()
        full_message_list = []
        for i, message in enumerate(messages):
            if message.id< message_id or (message_id==-1 and i<len(messages)-1): 
                if message.message_type<=MSG_TYPE.MSG_TYPE_FULL_INVISIBLE_TO_USER.value and message.message_type!=MSG_TYPE.MSG_TYPE_FULL_INVISIBLE_TO_AI.value:
                    full_message_list.append("\n"+self.config.discussion_prompt_separator+message.sender+": "+message.content.strip())
            else:
                break

        link_text = "\n" #self.personality.link_text
        if not is_continue:
            full_message_list.append("\n"+self.config.discussion_prompt_separator +message.sender.replace(":","")+": "+message.content.strip()+link_text+self.personality.ai_message_prefix)
        else:
            full_message_list.append("\n"+self.config.discussion_prompt_separator +message.sender.replace(":","")+": "+message.content.strip())


        composed_messages = link_text.join(full_message_list)
        t = self.model.tokenize(composed_messages)
        cond_tk = self.model.tokenize(self.personality.personality_conditioning)
        n_t = len(t)
        n_cond_tk = len(cond_tk)
        max_prompt_stx_size = 3*int(self.config.ctx_size/4)
        if n_cond_tk+n_t>max_prompt_stx_size:
            nb_tk = max_prompt_stx_size-n_cond_tk
            composed_messages = self.model.detokenize(t[-nb_tk:])
            ASCIIColors.warning(f"Cropping discussion to fit context [using {nb_tk} tokens/{self.config.ctx_size}]")
        discussion_messages = self.personality.personality_conditioning+ composed_messages
        tokens = self.model.tokenize(discussion_messages)
        
        if self.config["debug"]:
            ASCIIColors.yellow(discussion_messages)
            ASCIIColors.yellow(f"prompt size:{len(tokens)} tokens")

        return discussion_messages, message.content, tokens

    def get_discussion_to(self, client_id,  message_id=-1):
        messages = self.connections[client_id]["current_discussion"].get_messages()
        full_message_list = []
        ump = self.config.discussion_prompt_separator +self.config.user_name+": " if self.config.use_user_name_in_discussions else self.personality.user_message_prefix

        for message in messages:
            if message["id"]<= message_id or message_id==-1: 
                if message["type"]!=MSG_TYPE.MSG_TYPE_FULL_INVISIBLE_TO_USER:
                    if message["sender"]==self.personality.name:
                        full_message_list.append(self.personality.ai_message_prefix+message["content"])
                    else:
                        full_message_list.append(ump + message["content"])

        link_text = "\n"# self.personality.link_text

        if len(full_message_list) > self.config["nb_messages_to_remember"]:
            discussion_messages = self.personality.personality_conditioning+ link_text.join(full_message_list[-self.config["nb_messages_to_remember"]:])
        else:
            discussion_messages = self.personality.personality_conditioning+ link_text.join(full_message_list)
        
        return discussion_messages # Removes the last return

    def remove_text_from_string(self, string, text_to_find):
        """
        Removes everything from the first occurrence of the specified text in the string (case-insensitive).

        Parameters:
        string (str): The original string.
        text_to_find (str): The text to find in the string.

        Returns:
        str: The updated string.
        """
        index = string.lower().find(text_to_find.lower())

        if index != -1:
            string = string[:index]

        return string
    
    def notify(self, content, status, client_id):
        self.socketio.emit('notification', {
                            'content': content,# self.connections[client_id]["generated_text"], 
                            'status': status
                        }, room=client_id
                        )        

    def new_message(self, 
                            client_id, 
                            sender, 
                            content, 
                            metadata=None, 
                            message_type:MSG_TYPE=MSG_TYPE.MSG_TYPE_FULL, 
                            sender_type:SENDER_TYPES=SENDER_TYPES.SENDER_TYPES_AI
                        ):
        
        msg = self.connections[client_id]["current_discussion"].add_message(
            message_type        = message_type.value,
            sender_type         = sender_type.value,
            sender              = sender,
            content             = content,
            metadata            = json.dumps(metadata, indent=4) if metadata is not None and type(metadata)== dict else metadata,
            rank                = 0,
            parent_message_id   = self.connections[client_id]["current_discussion"].current_message.id,
            binding             = self.config["binding_name"],
            model               = self.config["model_name"], 
            personality         = self.config["personalities"][self.config["active_personality_id"]],
        )  # first the content is empty, but we'll fill it at the end  

        self.socketio.emit('new_message',
                {
                    "sender":                   self.personality.name,
                    "message_type":             message_type.value,
                    "sender_type":              SENDER_TYPES.SENDER_TYPES_AI.value,
                    "content":                  content,
                    "metadata":                 json.dumps(metadata, indent=4) if metadata is not None and type(metadata)== dict else metadata,
                    "id":                       msg.id,
                    "parent_message_id":        msg.parent_message_id,

                    'binding':                  self.config["binding_name"],
                    'model' :                   self.config["model_name"], 
                    'personality':              self.config["personalities"][self.config["active_personality_id"]],

                    'created_at':               self.connections[client_id]["current_discussion"].current_message.created_at,
                    'finished_generating_at':   self.connections[client_id]["current_discussion"].current_message.finished_generating_at,                        
                }, room=client_id
        )

    def update_message(self, client_id, chunk, metadata, msg_type:MSG_TYPE=None):
        self.connections[client_id]["current_discussion"].current_message.finished_generating_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.socketio.emit('update_message', {
                                        "sender": self.personality.name,
                                        'id':self.connections[client_id]["current_discussion"].current_message.id, 
                                        'content': chunk,# self.connections[client_id]["generated_text"], 
                                        'discussion_id':self.connections[client_id]["current_discussion"].discussion_id,
                                        'message_type': msg_type.value if msg_type is not None else MSG_TYPE.MSG_TYPE_CHUNK.value if self.nb_received_tokens>1 else MSG_TYPE.MSG_TYPE_FULL.value,
                                        'finished_generating_at': self.connections[client_id]["current_discussion"].current_message.finished_generating_at,
                                        'metadata':json.dumps(metadata, indent=4) if metadata is not None and type(metadata)== dict else metadata
                                    }, room=client_id
                            )
        self.socketio.sleep(0.01)
        self.connections[client_id]["current_discussion"].update_message(self.connections[client_id]["generated_text"])

    def close_message(self, client_id):
        # Send final message
        self.connections[client_id]["current_discussion"].current_message.finished_generating_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.socketio.emit('close_message', {
                                        "sender": self.personality.name,
                                        "id": self.connections[client_id]["current_discussion"].current_message.id,
                                        "content":self.connections[client_id]["generated_text"],

                                        'binding': self.config["binding_name"],
                                        'model' : self.config["model_name"], 
                                        'personality':self.config["personalities"][self.config["active_personality_id"]],

                                        'created_at': self.connections[client_id]["current_discussion"].current_message.created_at,
                                        'finished_generating_at': self.connections[client_id]["current_discussion"].current_message.finished_generating_at,

                                    }, room=client_id
                            )
    def process_chunk(self, chunk, message_type:MSG_TYPE, metadata:dict={}, client_id:int=0):
        """
        0 : a regular message
        1 : a notification message
        2 : A hidden message
        """

        if message_type == MSG_TYPE.MSG_TYPE_STEP:
            ASCIIColors.info("--> Step:"+chunk)
        if message_type == MSG_TYPE.MSG_TYPE_STEP_START:
            ASCIIColors.info("--> Step started:"+chunk)
        if message_type == MSG_TYPE.MSG_TYPE_STEP_END:
            if metadata['status']:
                ASCIIColors.success("--> Step ended:"+chunk)
            else:
                ASCIIColors.error("--> Step ended:"+chunk)
        if message_type == MSG_TYPE.MSG_TYPE_EXCEPTION:
            self.notify(chunk,False, client_id)
            ASCIIColors.error("--> Exception from personality:"+chunk)
        if message_type == MSG_TYPE.MSG_TYPE_WARNING:
            self.notify(chunk,True, client_id)
            ASCIIColors.error("--> Exception from personality:"+chunk)
        if message_type == MSG_TYPE.MSG_TYPE_INFO:
            self.notify(chunk,True, client_id)
            ASCIIColors.info("--> Info:"+chunk)

        if message_type == MSG_TYPE.MSG_TYPE_NEW_MESSAGE:
            self.nb_received_tokens = 0
            self.new_message(client_id, self.personality.name, chunk, metadata = metadata["metadata"], message_type= MSG_TYPE(metadata["type"]))

        elif message_type == MSG_TYPE.MSG_TYPE_FINISHED_MESSAGE:
            self.close_message(client_id)

        elif message_type == MSG_TYPE.MSG_TYPE_CHUNK:
            self.connections[client_id]["generated_text"] += chunk
            self.nb_received_tokens += 1
            ASCIIColors.green(f"Received {self.nb_received_tokens} tokens",end="\r")
            sys.stdout = sys.__stdout__
            sys.stdout.flush()
            antiprompt = self.personality.detect_antiprompt(self.connections[client_id]["generated_text"])
            if antiprompt:
                ASCIIColors.warning(f"\nDetected hallucination with antiprompt: {antiprompt}")
                self.connections[client_id]["generated_text"] = self.remove_text_from_string(self.connections[client_id]["generated_text"],antiprompt)
                self.update_message(client_id, self.connections[client_id]["generated_text"], metadata,MSG_TYPE.MSG_TYPE_FULL)
                return False
            else:
                self.update_message(client_id, chunk, metadata)
                # if stop generation is detected then stop
                if not self.cancel_gen:
                    return True
                else:
                    self.cancel_gen = False
                    ASCIIColors.warning("Generation canceled")
                    return False
 
        # Stream the generated text to the main process
        elif message_type == MSG_TYPE.MSG_TYPE_FULL:
            self.connections[client_id]["generated_text"] = chunk
            self.nb_received_tokens += 1
            ASCIIColors.green(f"Received {self.nb_received_tokens} tokens",end="\r",flush=True)
            self.update_message(client_id, chunk, metadata)
            return True
        # Stream the generated text to the frontend
        else:
            self.update_message(client_id, chunk, metadata, message_type)
        return True


    def generate(self, full_prompt, prompt, n_predict, client_id, callback=None):
        if self.personality.processor is not None:
            ASCIIColors.success("Running workflow")
            try:
                self.personality.processor.run_workflow( prompt, full_prompt, callback)
            except Exception as ex:
                # Catch the exception and get the traceback as a list of strings
                traceback_lines = traceback.format_exception(type(ex), ex, ex.__traceback__)
                # Join the traceback lines into a single string
                traceback_text = ''.join(traceback_lines)
                ASCIIColors.error(f"Workflow run failed.\nError:{ex}")
                ASCIIColors.error(traceback_text)
                if callback:
                    callback(f"Workflow run failed\nError:{ex}", MSG_TYPE.MSG_TYPE_EXCEPTION)                   
            print("Finished executing the workflow")
            return

        self._generate(full_prompt, n_predict, client_id, callback)
        ASCIIColors.success("\nFinished executing the generation")

    def _generate(self, prompt, n_predict, client_id, callback=None):
        self.nb_received_tokens = 0
        if self.model is not None:
            ASCIIColors.info(f"warmup for generating {n_predict} tokens")
            if self.config["override_personality_model_parameters"]:
                output = self.model.generate(
                    prompt,
                    callback=callback,
                    n_predict=n_predict,
                    temperature=self.config['temperature'],
                    top_k=self.config['top_k'],
                    top_p=self.config['top_p'],
                    repeat_penalty=self.config['repeat_penalty'],
                    repeat_last_n = self.config['repeat_last_n'],
                    seed=self.config['seed'],
                    n_threads=self.config['n_threads']
                )
            else:
                output = self.model.generate(
                    prompt,
                    callback=callback,
                    n_predict=min(n_predict,self.personality.model_n_predicts),
                    temperature=self.personality.model_temperature,
                    top_k=self.personality.model_top_k,
                    top_p=self.personality.model_top_p,
                    repeat_penalty=self.personality.model_repeat_penalty,
                    repeat_last_n = self.personality.model_repeat_last_n,
                    seed=self.config['seed'],
                    n_threads=self.config['n_threads']
                )
        else:
            print("No model is installed or selected. Please make sure to install a model and select it inside your configuration before attempting to communicate with the model.")
            print("To do this: Install the model to your models/<binding name> folder.")
            print("Then set your model information in your local configuration file that you can find in configs/local_config.yaml")
            print("You can also use the ui to set your model in the settings page.")
            output = ""
        return output
                     
    def start_message_generation(self, message, message_id, client_id, is_continue=False):

        ASCIIColors.info(f"Text generation requested by client: {client_id}")
        # send the message to the bot
        print(f"Received message : {message.content}")
        if self.connections[client_id]["current_discussion"]:
            if not self.model:
                self.notify("No model selected. Please make sure you select a model before starting generation", False, client_id)
                return          
            # First we need to send the new message ID to the client
            if is_continue:
                self.connections[client_id]["current_discussion"].load_message(message_id)
                self.connections[client_id]["generated_text"] = message.content
            else:
                self.new_message(client_id, self.personality.name, "✍ please stand by ...")
            self.socketio.sleep(0.01)

            # prepare query and reception
            self.discussion_messages, self.current_message, tokens = self.prepare_query(client_id, message_id, is_continue)
            self.prepare_reception(client_id)
            self.generating = True
            self.connections[client_id]["processing"]=True
            self.generate(
                            self.discussion_messages, 
                            self.current_message, 
                            n_predict = self.config.ctx_size-len(tokens)-1,
                            client_id=client_id,
                            callback=partial(self.process_chunk,client_id = client_id)
                        )
            print()
            print("## Done Generation ##")
            print()
            self.cancel_gen = False

            # Send final message
            self.close_message(client_id)
            self.socketio.sleep(0.01)
            self.connections[client_id]["processing"]=False
            if self.connections[client_id]["schedule_for_deletion"]:
                del self.connections[client_id]

            ASCIIColors.success(f" ╔══════════════════════════════════════════════════╗ ")
            ASCIIColors.success(f" ║                        Done                      ║ ")
            ASCIIColors.success(f" ╚══════════════════════════════════════════════════╝ ")
        else:
            ump = self.config.discussion_prompt_separator +self.config.user_name+": " if self.config.use_user_name_in_discussions else self.personality.user_message_prefix
            
            self.cancel_gen = False
            #No discussion available
            ASCIIColors.warning("No discussion selected!!!")

            self.notify("No discussion selected!!!",False, client_id)
            
            print()
            return ""
    