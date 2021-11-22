#!/usr/bin/env python3
# -*-coding:UTF-8 -*

import os
import json
import secrets
import re
import sys
import time
import uuid

from flask import escape

sys.path.append(os.path.join(os.environ['AIL_BIN'], 'lib/'))
import ConfigLoader

sys.path.append(os.path.join(os.environ['AIL_BIN'], 'core/'))
import screen

sys.path.append(os.path.join(os.environ['AIL_BIN'], 'packages/'))
from Item import Item
import Tag

config_loader = ConfigLoader.ConfigLoader()
r_cache = config_loader.get_redis_conn("Redis_Cache")
r_serv_db = config_loader.get_redis_conn("ARDB_DB")
r_serv_sync = config_loader.get_redis_conn("ARDB_DB")
config_loader = None

def is_valid_uuid_v4(UUID):
    if not UUID:
        return False
    UUID = UUID.replace('-', '')
    try:
        uuid_test = uuid.UUID(hex=UUID, version=4)
        return uuid_test.hex == UUID
    except:
        return False

def sanityze_uuid(UUID):
    sanityzed_uuid = uuid.UUID(hex=UUID, version=4)
    return str(sanityzed_uuid)

def generate_uuid():
    return str(uuid.uuid4()).replace('-', '')

def generate_sync_api_key():
    return secrets.token_urlsafe(42)

def get_ail_uuid():
    return r_serv_db.get('ail:uuid')

def is_valid_websocket_url(websocket_url):
    regex_websocket_url = r'^(wss:\/\/)([0-9]{1,3}(?:\.[0-9]{1,3}){3}|(?=[^\/]{1,254}(?![^\/]))(?:(?=[a-zA-Z0-9-]{1,63}\.?)(?:xn--+)?[a-zA-Z0-9]+(?:-[a-zA-Z0-9]+)*\.?)+[a-zA-Z]{2,63}):([0-9]{1,5})$'
    if re.match(regex_websocket_url, websocket_url):
        return True
    return False

def is_valid_websocket_key(ail_key):
    regex_key = r'^[A-Za-z0-9-_]{56}$'
    if re.match(regex_key, ail_key):
        return True
    return False

#### HANDLE CONFIG UPDATE ####

def get_last_updated_sync_config():
    epoch = r_serv_sync.get(f'ail:instance:queue:last_updated_sync_config')
    if not epoch:
        epoch = 0
    return float(epoch)

def set_last_updated_sync_config():
    epoch = int(time.time())
    r_serv_sync.set(f'ail:instance:queue:last_updated_sync_config', epoch)
    return epoch

# # TODO: get connection status
# # TODO: get connection METADATA
#############################
#                           #
#### SYNC CLIENT MANAGER ####

def get_all_sync_clients(r_set=False):
    res = r_cache.smembers('ail_2_ail:all_sync_clients')
    if r_set:
        return set(res)
    else:
        return res

def get_sync_client_ail_uuid(client_id):
    return r_cache.hget(f'ail_2_ail:sync_client:{client_id}', 'ail_uuid')

# current: only one push registred
def get_client_id_by_ail_uuid(ail_uuid):
    res = r_cache.smembers(f'ail_2_ail:ail_uuid:{ail_uuid}')
    if res:
        return int(res.pop())

def get_all_running_sync_servers():
    running_ail_servers= []
    for client_id in get_all_sync_clients():
        ail_uuid = get_sync_client_ail_uuid(client_id)
        running_ail_servers.append(ail_uuid)
    return running_ail_servers

def delete_sync_client_cache(client_id):
    ail_uuid = get_sync_client_ail_uuid(client_id)
    # map ail_uuid/queue_uuid
    r_cache.srem(f'ail_2_ail:ail_uuid:{ail_uuid}', client_id)
    r_cache.srem(f'ail_2_ail:queue_uuid:{queue_uuid}', client_id)

    r_cache.delete(f'ail_2_ail:sync_client:{client_id}')
    r_cache.srem('ail_2_ail:all_sync_clients', client_id)

def delete_all_sync_clients_cache():
    for client_id in get_all_sync_clients():
        delete_sync_client_cache(client_id)
    r_cache.delete('ail_2_ail:all_sync_clients')

# command: -launch
#          -kill
#          -relaunch
## TODO: check command
def send_command_to_manager(command, client_id=-1, ail_uuid=None):
    dict_action = {'command': command, 'client_id': client_id}
    if ail_uuid:
        dict_action['ail_uuid'] = ail_uuid
    str_command = json.dumps(dict_action)
    r_cache.sadd('ail_2_ail:client_manager:command', str_command)


def refresh_ail_instance_connection(ail_uuid):
    client_id = get_client_id_by_ail_uuid(ail_uuid)
    launch_required = is_ail_instance_push_enabled(ail_uuid)

    print(client_id)
    print(launch_required)

    # relaunch
    if client_id and launch_required:
        send_command_to_manager('relaunch', client_id=client_id)
    # kill
    elif client_id:
        send_command_to_manager('kill', client_id=client_id)
    # launch
    elif launch_required:
        send_command_to_manager('launch', ail_uuid=ail_uuid)


class AIL2AILClientManager(object):
    """AIL2AILClientManager."""

    SCREEN_NAME = 'AIL_2_AIL'
    SCRIPT_NAME = 'ail_2_ail_client.py'
    SCRIPT_DIR = os.path.join(os.environ['AIL_BIN'], 'core')

    def __init__(self):
        # dict client_id: AIL2AILCLIENT or websocket
        self.clients = {}
        # launch all sync clients
        self.relaunch_all_sync_clients()

    def get_all_clients(self):
        return self.clients

    # return new client id
    def get_new_sync_client_id(self):
        for new_id in range(1, 100000):
            new_id = str(new_id)
            if new_id not in self.clients:
                return str(new_id)

    def get_sync_client_ail_uuid(self, client_id):
        return self.clients[client_id]['ail_uuid']

    # def get_sync_client_queue_uuid(self, client_id):
    #     return self.clients[client_id]['queue_uuid']

    def get_all_sync_clients_to_launch(self):
        ail_instances_to_launch = []
        for ail_uuid in get_all_ail_instance():
            if is_ail_instance_push_enabled(ail_uuid):
                ail_instances_to_launch.append(ail_uuid)
        return ail_instances_to_launch

    def relaunch_all_sync_clients(self):
        delete_all_sync_clients_cache()
        self.clients = {}
        for ail_uuid in self.get_all_sync_clients_to_launch():
             self.launch_sync_client(ail_uuid)

    def launch_sync_client(self, ail_uuid):
        dir_project = os.environ['AIL_HOME']
        client_id = self.get_new_sync_client_id()
        script_options = f'-a {ail_uuid} -m push -i {client_id}'
        screen.create_screen(AIL2AILClientManager.SCREEN_NAME)
        screen.launch_uniq_windows_script(AIL2AILClientManager.SCREEN_NAME,
                                            client_id, dir_project,
                                            AIL2AILClientManager.SCRIPT_DIR,
                                            AIL2AILClientManager.SCRIPT_NAME,
                                            script_options=script_options, kill_previous_windows=True)
        # save sync client status
        r_cache.hset(f'ail_2_ail:sync_client:{client_id}', 'ail_uuid', ail_uuid)
        r_cache.hset(f'ail_2_ail:sync_client:{client_id}', 'launch_time', int(time.time()))

        r_cache.sadd('ail_2_ail:all_sync_clients', client_id)

        # create map ail_uuid/queue_uuid
        r_cache.sadd(f'ail_2_ail:ail_uuid:{ail_uuid}', client_id)

        self.clients[client_id] = {'ail_uuid': ail_uuid}

    # # TODO: FORCE KILL ????????????
    # # TODO: check if exists
    def kill_sync_client(self, client_id):
        if not screen.kill_screen_window('AIL_2_AIL',client_id):
            # # TODO: log kill error
            pass

        delete_sync_client_cache(client_id)
        self.clients.pop(client_id)

    ## COMMANDS ##

    def get_manager_command(self):
        res = r_cache.spop('ail_2_ail:client_manager:command')
        if res:
            print(res)
            print(type(res))
            return json.loads(res)
        else:
            return None

    def execute_manager_command(self, command_dict):
        print(command_dict)
        command = command_dict.get('command')
        if command == 'launch':
            ail_uuid = command_dict.get('ail_uuid')
            self.launch_sync_client(ail_uuid)
        elif command == 'relaunch_all':
            self.relaunch_all_sync_clients()
        else:
            # only one sync client
            client_id = int(command_dict.get('client_id'))
            if client_id < 1:
                print('Invalid client id')
                return None
            client_id = str(client_id)
            if command == 'kill':
                self.kill_sync_client(client_id)
            elif command == 'relaunch':
                ail_uuid = self.get_sync_client_ail_uuid(client_id)
                self.kill_sync_client(client_id)
                self.launch_sync_client(ail_uuid)

########################################
########################################
########################################

# # TODO: ADD METADATA
def get_sync_client_status(client_id):
    dict_client = {'id': client_id}
    dict_client['ail_uuid'] = get_sync_client_ail_uuid(client_id)
    return dict_client

def get_all_sync_client_status():
    sync_clients = []
    all_sync_clients = r_cache.smembers('ail_2_ail:all_sync_clients')
    for client_id in all_sync_clients:
        sync_clients.append(get_sync_client_status(client_id))
    return sync_clients

######################
#                    #
#### AIL INSTANCE ####

## AIL KEYS ##

def get_all_ail_instance_keys():
    return r_serv_sync.smembers(f'ail:instance:key:all')

def is_allowed_ail_instance_key(key):
    return r_serv_sync.sismember(f'ail:instance:key:all', key)

def get_ail_instance_key(ail_uuid):
    return r_serv_sync.hget(f'ail:instance:{ail_uuid}', 'api_key')

def get_ail_instance_by_key(key):
    return r_serv_sync.get(f'ail:instance:key:{key}')

# def check_acl_sync_queue_ail(ail_uuid, queue_uuid, key):
#     return is_ail_instance_queue(ail_uuid, queue_uuid)

def update_ail_instance_key(ail_uuid, new_key):
    old_key = get_ail_instance_key(ail_uuid)
    r_serv_sync.srem(f'ail:instance:key:all', old_key)
    r_serv_sync.delete(f'ail:instance:key:{old_key}')

    r_serv_sync.sadd(f'ail:instance:key:all', new_key)
    r_serv_sync.delete(f'ail:instance:key:{new_key}', ail_uuid)
    r_serv_sync.hset(f'ail:instance:{ail_uuid}', 'api_key', new_key)

#- AIL KEYS -#

def get_all_ail_instance():
    return r_serv_sync.smembers('ail:instance:all')

def get_ail_instance_all_sync_queue(ail_uuid):
    return r_serv_sync.smembers(f'ail:instance:sync_queue:{ail_uuid}')

def is_ail_instance_queue(ail_uuid, queue_uuid):
    return r_serv_sync.sismember(f'ail:instance:sync_queue:{ail_uuid}', queue_uuid)

def exists_ail_instance(ail_uuid):
    return r_serv_sync.exists(f'ail:instance:{ail_uuid}')

def get_ail_instance_url(ail_uuid):
    return r_serv_sync.hget(f'ail:instance:{ail_uuid}', 'url')

def get_ail_instance_description(ail_uuid):
    return r_serv_sync.hget(f'ail:instance:{ail_uuid}', 'description')

def exists_ail_instance(ail_uuid):
    return r_serv_sync.sismember('ail:instance:all', ail_uuid)

def is_ail_instance_push_enabled(ail_uuid):
    res = r_serv_sync.hget(f'ail:instance:{ail_uuid}', 'push')
    return res == 'True'

def is_ail_instance_pull_enabled(ail_uuid):
    res = r_serv_sync.hget(f'ail:instance:{ail_uuid}', 'pull')
    return res == 'True'

def is_ail_instance_sync_enabled(ail_uuid, sync_mode=None):
    if sync_mode is None:
        return is_ail_instance_push_enabled(ail_uuid) or is_ail_instance_pull_enabled(ail_uuid)
    elif sync_mode == 'pull':
        return is_ail_instance_pull_enabled(ail_uuid)
    elif sync_mode == 'push':
        return is_ail_instance_push_enabled(ail_uuid)
    else:
        return False

def change_pull_push_state(ail_uuid, pull=False, push=False):
    # sanityze pull/push
    if pull:
        pull = True
    else:
        pull = False
    if push:
        push = True
    else:
        push = False
    r_serv_sync.hset(f'ail:instance:{ail_uuid}', 'push', push)
    r_serv_sync.hset(f'ail:instance:{ail_uuid}', 'pull', pull)
    set_last_updated_sync_config()
    refresh_ail_instance_connection(ail_uuid)

# # TODO: HIDE ADD GLOBAL FILTER (ON BOTH SIDE)
def get_ail_instance_metadata(ail_uuid, sync_queues=False):
    dict_meta = {}
    dict_meta['uuid'] = ail_uuid
    dict_meta['url'] = get_ail_instance_url(ail_uuid)
    dict_meta['description'] = get_ail_instance_description(ail_uuid)
    dict_meta['pull'] = is_ail_instance_pull_enabled(ail_uuid)
    dict_meta['push'] = is_ail_instance_pull_enabled(ail_uuid)

    # # TODO: HIDE
    dict_meta['api_key'] = get_ail_instance_key(ail_uuid)

    if sync_queues:
        dict_meta['sync_queues'] = get_ail_instance_all_sync_queue(ail_uuid)

    # # TODO:
    # - set UUID sync_queue

    return dict_meta

def get_all_ail_instances_metadata():
    l_servers = []
    for ail_uuid in get_all_ail_instance():
        l_servers.append(get_ail_instance_metadata(ail_uuid, sync_queues=True))
    return l_servers

def get_ail_instances_metadata(l_ail_servers):
    l_servers = []
    for ail_uuid in l_ail_servers:
        l_servers.append(get_ail_instance_metadata(ail_uuid, sync_queues=True))
    return l_servers

# # TODO: VALIDATE URL
#                  API KEY
def create_ail_instance(ail_uuid, url, api_key=None, description=None, pull=True, push=True):
    r_serv_sync.sadd('ail:instance:all', ail_uuid)
    r_serv_sync.hset(f'ail:instance:{ail_uuid}', 'url', url)
    ## API KEY ##
    if not api_key:
        api_key = generate_sync_api_key()
    r_serv_sync.hset(f'ail:instance:{ail_uuid}', 'api_key', api_key)
    r_serv_sync.sadd('ail:instance:key:all', api_key)
    r_serv_sync.set(f'ail:instance:key:{api_key}', ail_uuid)
    #- API KEY -#
    if description:
        r_serv_sync.hset(f'ail:instance:{ail_uuid}', 'description', description)
    change_pull_push_state(ail_uuid, pull=pull, push=push)
    set_last_updated_sync_config()
    refresh_ail_instance_connection(ail_uuid)
    return ail_uuid

def delete_ail_instance(ail_uuid):
    for queue_uuid in get_ail_instance_all_sync_queue(ail_uuid):
        unregister_ail_to_sync_queue(ail_uuid, queue_uuid)
    r_serv_sync.delete(f'ail:instance:sync_queue:{ail_uuid}')
    key = get_ail_instance_by_key(ail_uuid)
    r_serv_sync.delete(f'ail:instance:{ail_uuid}')
    r_serv_sync.srem('ail:instance:key:all', ail_uuid)
    r_serv_sync.delete(f'ail:instance:key:{key}', ail_uuid)
    r_serv_sync.srem('ail:instance:all', ail_uuid)
    set_last_updated_sync_config()
    refresh_ail_instance_connection(ail_uuid)
    return ail_uuid

## API ##

def api_create_ail_instance(json_dict):
    ail_uuid = json_dict.get('uuid').replace(' ', '')
    if not is_valid_uuid_v4(ail_uuid):
        return {"status": "error", "reason": "Invalid ail uuid"}, 400
    ail_uuid = sanityze_uuid(ail_uuid)
    if exists_ail_instance(ail_uuid):
        return {"status": "error", "reason": "AIL uuid already exists"}, 400

    if json_dict.get('pull'):
        pull = True
    else:
        pull = False
    if json_dict.get('push'):
        push = True
    else:
        push = False
    description = json_dict.get('description')

    ail_url = json_dict.get('url').replace(' ', '')
    if not is_valid_websocket_url(ail_url):
        return {"status": "error", "reason": "Invalid websocket url"}, 400

    ail_key = json_dict.get('key')
    if ail_key:
        ail_key = ail_key.replace(' ', '')
        if not is_valid_websocket_key(ail_key):
            return {"status": "error", "reason": "Invalid websocket key"}, 400

    res = create_ail_instance(ail_uuid, ail_url, api_key=ail_key, description=description,
                                pull=pull, push=push)
    return res, 200

def api_delete_ail_instance(json_dict):
    ail_uuid = json_dict.get('uuid', '').replace(' ', '')
    if not is_valid_uuid_v4(ail_uuid):
        return {"status": "error", "reason": "Invalid AIL uuid"}, 400
    ail_uuid = sanityze_uuid(ail_uuid)
    if not exists_ail_instance(ail_uuid):
        return {"status": "error", "reason": "AIL server not found"}, 404

    res = delete_ail_instance(ail_uuid)
    return res, 200

####################
#                  #
#### SYNC QUEUE ####

class Sync_Queue(object): # # TODO: use for edit
    """Sync_Queue."""

    def __init__(self, uuid):
        self.uuid = uuid

def get_all_sync_queue():
    return r_serv_sync.smembers('ail2ail:sync_queue:all')

def get_sync_queue_all_ail_instance(queue_uuid):
    return r_serv_sync.smembers(f'ail2ail:sync_queue:ail_instance:{queue_uuid}')

def exists_sync_queue(queue_uuid):
    return r_serv_sync.exists(f'ail2ail:sync_queue:{queue_uuid}')

# # TODO: check if push or pull enabled ?
def is_queue_used_by_ail_instance(queue_uuid):
    return r_serv_sync.exists(f'ail2ail:sync_queue:ail_instance:{queue_uuid}')

# # TODO: add others filter
def get_sync_queue_filter(queue_uuid):
    return r_serv_sync.smembers(f'ail2ail:sync_queue:filter:tags:{queue_uuid}')

def get_sync_queue_name(queue_uuid):
    return r_serv_sync.hget(f'ail2ail:sync_queue:{queue_uuid}', 'name')

def get_sync_queue_description(queue_uuid):
    return r_serv_sync.hget(f'ail2ail:sync_queue:{queue_uuid}', 'description')

def get_sync_queue_max_size(queue_uuid):
    return r_serv_sync.hget(f'ail2ail:sync_queue:{queue_uuid}', 'max_size')

# # TODO: ADD FILTER
def get_sync_queue_metadata(queue_uuid):
    dict_meta = {}
    dict_meta['uuid'] = queue_uuid
    dict_meta['name'] = get_sync_queue_name(queue_uuid)
    dict_meta['description'] = get_sync_queue_description(queue_uuid)
    dict_meta['max_size'] = get_sync_queue_max_size(queue_uuid)
    dict_meta['tags'] = get_sync_queue_filter(queue_uuid)

    # # TODO: TO ADD:
    # - get uuid instance

    return dict_meta

def get_all_queues_metadata():
    l_queues = []
    for queue_uuid in get_all_sync_queue():
        l_queues.append(get_sync_queue_metadata(queue_uuid))
    return l_queues

def get_queues_metadata(l_queues_uuid):
    l_queues = []
    for queue_uuid in l_queues_uuid:
        l_queues.append(get_sync_queue_metadata(queue_uuid))
    return l_queues

#####################################################
def get_all_sync_queue_dict():
    dict_sync_queues = {}
    for queue_uuid in get_all_sync_queue():
        if is_queue_used_by_ail_instance(queue_uuid):
            dict_queue = {}
            dict_queue['filter'] = get_sync_queue_filter(queue_uuid)

            dict_queue['ail_instances'] = [] ############ USE DICT ?????????
            for ail_uuid in get_sync_queue_all_ail_instance(queue_uuid):
                dict_ail = {'ail_uuid': ail_uuid,
                            'pull': is_ail_instance_pull_enabled(ail_uuid),
                            'push': is_ail_instance_push_enabled(ail_uuid)}
                if dict_ail['pull'] or dict_ail['push']:
                    dict_queue['ail_instances'].append(dict_ail)
            if dict_queue['ail_instances']:
                dict_sync_queues[queue_uuid] = dict_queue
    return dict_sync_queues

def is_queue_registred_by_ail_instance(queue_uuid, ail_uuid):
    return r_serv_sync.sismember(f'ail:instance:sync_queue:{ail_uuid}', queue_uuid)

def register_ail_to_sync_queue(ail_uuid, queue_uuid):
    r_serv_sync.sadd(f'ail2ail:sync_queue:ail_instance:{queue_uuid}', ail_uuid)
    r_serv_sync.sadd(f'ail:instance:sync_queue:{ail_uuid}', queue_uuid)
    set_last_updated_sync_config()

# # # FIXME: TODO: delete sync queue ????????????????????????????????????????????????????
def unregister_ail_to_sync_queue(ail_uuid, queue_uuid):
    r_serv_sync.srem(f'ail2ail:sync_queue:ail_instance:{queue_uuid}', ail_uuid)
    r_serv_sync.srem(f'ail:instance:sync_queue:{ail_uuid}', queue_uuid)
    set_last_updated_sync_config()

def get_all_unregistred_queue_by_ail_instance(ail_uuid):
    return r_serv_sync.sdiff('ail2ail:sync_queue:all', f'ail:instance:sync_queue:{ail_uuid}')

# # TODO: optionnal name ???
# # TODO: SANITYZE TAGS
def create_sync_queue(name, tags=[], description=None, max_size=100):
    queue_uuid = generate_uuid()
    r_serv_sync.sadd('ail2ail:sync_queue:all', queue_uuid)

    r_serv_sync.hset(f'ail2ail:sync_queue:{queue_uuid}', 'name', name)
    if description:
        r_serv_sync.hset(f'ail2ail:sync_queue:{queue_uuid}', 'description', description)
    r_serv_sync.hset(f'ail2ail:sync_queue:{queue_uuid}', 'max_size', max_size)

    for tag in tags:
        r_serv_sync.sadd(f'ail2ail:sync_queue:filter:tags:{queue_uuid}', tag)

    set_last_updated_sync_config()
    return queue_uuid

def delete_sync_queue(queue_uuid):
    for ail_uuid in get_sync_queue_all_ail_instance(queue_uuid):
        unregister_ail_to_sync_queue(ail_uuid, queue_uuid)
    r_serv_sync.delete(f'ail2ail:sync_queue:{queue_uuid}')
    r_serv_sync.delete(f'ail2ail:sync_queue:filter:tags:{queue_uuid}')
    r_serv_sync.srem('ail2ail:sync_queue:all', queue_uuid)
    set_last_updated_sync_config()
    return queue_uuid

## API ##

# # TODO: sanityze queue_name
def api_create_sync_queue(json_dict):
    description = json_dict.get('description')
    description = escape(description)
    queue_name = json_dict.get('name')
    if queue_name: #################################################
        queue_name = escape(queue_name)

    tags = json_dict.get('tags')
    if not tags:
        {"status": "error", "reason": "no tags provided"}, 400
    if not Tag.are_enabled_tags(tags):
        {"status": "error", "reason": "Invalid/Disabled tags"}, 400

    max_size = json_dict.get('max_size')
    if not max_size:
        max_size = 100
    try:
        max_size = int(max_size)
    except ValueError:
        {"status": "error", "reason": "Invalid queue size value"}, 400
    if not max_size > 0:
        return {"status": "error", "reason": "Invalid queue size value"}, 400

    queue_uuid = create_sync_queue(queue_name, tags=tags, description=description,
                                    max_size=max_size)
    return queue_uuid, 200

def api_delete_sync_queue(json_dict):
    queue_uuid = json_dict.get('uuid', '').replace(' ', '').replace('-', '')
    if not is_valid_uuid_v4(queue_uuid):
        return {"status": "error", "reason": "Invalid Queue uuid"}, 400
    if not exists_sync_queue(queue_uuid):
        return {"status": "error", "reason": "Queue Sync not found"}, 404

    res = delete_sync_queue(queue_uuid)
    return res, 200

def api_register_ail_to_sync_queue(json_dict):
    ail_uuid = json_dict.get('ail_uuid', '').replace(' ', '')
    if not is_valid_uuid_v4(ail_uuid):
        return {"status": "error", "reason": "Invalid AIL uuid"}, 400
    ail_uuid = sanityze_uuid(ail_uuid)
    queue_uuid = json_dict.get('queue_uuid', '').replace(' ', '').replace('-', '')
    if not is_valid_uuid_v4(queue_uuid):
        return {"status": "error", "reason": "Invalid Queue uuid"}, 400

    if not exists_ail_instance(ail_uuid):
        return {"status": "error", "reason": "AIL server not found"}, 404
    if not exists_sync_queue(queue_uuid):
        return {"status": "error", "reason": "Queue Sync not found"}, 404
    if is_queue_registred_by_ail_instance(queue_uuid, ail_uuid):
        return {"status": "error", "reason": "Queue already registred"}, 400

    res = register_ail_to_sync_queue(ail_uuid, queue_uuid)
    return res, 200

def api_unregister_ail_to_sync_queue(json_dict):
    ail_uuid = json_dict.get('ail_uuid', '').replace(' ', '')
    if not is_valid_uuid_v4(ail_uuid):
        return {"status": "error", "reason": "Invalid ail uuid"}, 400
    ail_uuid = sanityze_uuid(ail_uuid)
    queue_uuid = json_dict.get('queue_uuid', '').replace(' ', '').replace('-', '')
    if not is_valid_uuid_v4(queue_uuid):
        return {"status": "error", "reason": "Invalid ail uuid"}, 400

    if not exists_ail_instance(ail_uuid):
        return {"status": "error", "reason": "AIL server not found"}, 404
    if not exists_sync_queue(queue_uuid):
        return {"status": "error", "reason": "Queue Sync not found"}, 404
    if not is_queue_registred_by_ail_instance(queue_uuid, ail_uuid):
        return {"status": "error", "reason": "Queue not registred"}, 400

    res = unregister_ail_to_sync_queue(ail_uuid, queue_uuid)
    return res, 200

#############################
#                           #
#### SYNC REDIS QUEUE #######

def get_sync_queue_object(ail_uuid, push=True):
    for queue_uuid in get_ail_instance_all_sync_queue(ail_uuid):
        obj_dict = get_sync_queue_object_by_queue_uuid(queue_uuid, ail_uuid, push=push)
        if obj_dict:
            return obj_dict
    return None

def get_sync_queue_object_by_queue_uuid(queue_uuid, ail_uuid, push=True):
    if push:
        sync_mode = 'push'
    else:
        sync_mode = 'pull'
    obj_dict = r_serv_sync.lpop(f'sync:queue:{sync_mode}:{queue_uuid}:{ail_uuid}')
    if obj_dict:
        obj_dict = json.loads(obj_dict)
        # # REVIEW: # TODO: create by obj type
        return Item(obj_dict['id'])

def add_object_to_sync_queue(queue_uuid, ail_uuid, obj_dict, push=True, pull=True):
    obj = json.dumps(obj_dict)

    # # TODO: # FIXME: USE CACHE ??????
    if push:
        r_serv_sync.lpush(f'sync:queue:push:{queue_uuid}:{ail_uuid}', obj)
        r_serv_sync.ltrim(f'sync:queue:push:{queue_uuid}:{ail_uuid}', 0, 200)

    if pull:
        r_serv_sync.lpush(f'sync:queue:pull:{queue_uuid}:{ail_uuid}', obj)
        r_serv_sync.ltrim(f'sync:queue:pull:{queue_uuid}:{ail_uuid}', 0, 200)

# # TODO: # REVIEW: USE CACHE ????? USE QUEUE FACTORY ?????
def get_sync_importer_ail_stream():
    return r_serv_sync.spop('sync:queue:importer')

def add_ail_stream_to_sync_importer(ail_stream):
    ail_stream = json.dumps(ail_stream)
    r_serv_sync.sadd('sync:queue:importer', ail_stream)

#############################
#                           #
#### AIL EXCHANGE FORMAT ####

def create_ail_stream(Object):
    ail_stream = {'format': 'ail',
                  'version': 1,
                  'type': Object.get_type()}

    # OBJECT META
    ail_stream['meta'] = {'ail_mime-type': 'text/plain'}
    ail_stream['meta']['ail:id'] = Object.get_id()
    ail_stream['meta']['ail:tags'] = Object.get_tags()
    # GLOBAL PAYLOAD
    ail_stream['meta']['ail:uuid'] = get_ail_uuid()

    # OBJECT PAYLOAD
    ail_stream['payload'] = Object.get_ail_2_ail_payload()

    return ail_stream

if __name__ == '__main__':

    ail_uuid = '03c51929-eeab-4d47-9dc0-c667f94c7d2d'
    url = "wss://localhost:4443"
    api_key = 'secret'
    #description = 'first test instance'
    queue_uuid = '79bcafc0a6d644deb2c75fb5a83d7caa'
    tags = ['infoleak:submission="manual"']
    name = 'submitted queue'
    description = 'first test queue, all submitted items'
    #queue_uuid = ''

    #res = create_ail_instance(ail_uuid, url, api_key=api_key, description=description)

    #res = create_sync_queue(name, tags=tags, description=description, max_size=100)
    #res = delete_sync_queue(queue_uuid)

    #res = register_ail_to_sync_queue(ail_uuid, queue_uuid)
    #res = change_pull_push_state(ail_uuid, push=True, pull=True)

    # print(get_ail_instance_all_sync_queue(ail_uuid))
    # print(get_all_sync_queue())
    # res = get_all_unregistred_queue_by_ail_instance(ail_uuid)

    ail_uuid = 'd82d3e61-2438-4ede-93bf-37b6fd9d7510'
    res = get_client_id_by_ail_uuid(ail_uuid)

    print(res)
