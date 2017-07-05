#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
    Blockstack-client
    ~~~~~
    copyright: (c) 2014-2015 by Halfmoon Labs, Inc.
    copyright: (c) 2016 by Blockstack.org

    This file is part of Blockstack-client.

    Blockstack-client is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    Blockstack-client is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.
    You should have received a copy of the GNU General Public License
    along with Blockstack-client. If not, see <http://www.gnu.org/licenses/>.
"""

import os
import random
import base64
import copy

# Hack around absolute paths
current_dir = os.path.abspath(os.path.dirname(__file__))
parent_dir = os.path.abspath(current_dir + "/../")

import json
import socket
import threading
import time
import tempfile
import hashlib
import keylib
from keylib import ECPrivateKey

import blockstack_zones
import virtualchain

from .queue import get_queue_state, in_queue, cleanup_preorder_queue, queue_removeall
from .queue import queue_find_accepted
from .queue import queue_add_error_msg

from .nameops import async_preorder, async_register, async_update, async_transfer, async_renew, async_revoke

from ..keys import get_data_privkey_info, is_singlesig_hex, get_owner_privkey_info, get_signing_privkey 
from ..token_file import token_file_put
from ..proxy import is_name_registered, is_zonefile_hash_current, get_default_proxy, get_name_blockchain_record, get_atlas_peers, json_is_error
from ..zonefile import zonefile_data_replicate
from ..user import is_user_zonefile
from ..storage import put_mutable_data, get_zonefile_data_hash

from ..constants import CONFIG_PATH, DEFAULT_QUEUE_PATH, BLOCKSTACK_DEBUG, BLOCKSTACK_TEST, TX_MIN_CONFIRMATIONS
from ..config import get_config
from ..utils import url_to_host_port
from ..logger import get_logger

DEBUG = True

__registrar_state = None
log = get_logger("blockstack-client-registrar")


def get_registrar_state(config_path=None, proxy=None):
    """
    Create singleton registrar state.
    """
    global __registrar_state
    if __registrar_state is None:
        raise Exception("State is not initialized")

    state = __registrar_state

    if config_path is None:
        config_path = state.config_path
        if config_path is None:
            config_path = CONFIG_PATH

    if proxy is None:
        proxy = get_default_proxy(config_path=config_path)

    return (state, config_path, proxy)


def set_registrar_state(config_path=None, wallet_keys=None):
    """
    Set singleton state and start the registrar thread.
    Return the registrar state on success
    Return None on error
    """
    global __registrar_state
    assert config_path is not None

    # if we're already running, then bail
    if RegistrarWorker.is_lockfile_valid( config_path ):
        log.debug("RegistrarWorker already initialized")
        return None

    log.info("Initialize Registrar State from %s" % (config_path))
    __registrar_state = RegistrarState(config_path)

    if wallet_keys:
        log.info("Setting wallet keys")

        res = set_wallet(
            (wallet_keys['payment_addresses'][0], wallet_keys['payment_privkey']),
            (wallet_keys['owner_addresses'][0], wallet_keys['owner_privkey']),
            (wallet_keys['data_pubkeys'][0], wallet_keys['data_privkey']),
            config_path=config_path
        )

        if 'error' in res:
            log.error("Failed to set wallet: {}".format(res['error']))
            __registrar_state = None
            return None

    __registrar_state.start()
    return __registrar_state


def registrar_shutdown(config_path=None):
    """
    Shut down existing state
    """
    global __registrar_state
    if __registrar_state is None:
        return

    log.info("Shut down Registrar State")
    __registrar_state.request_stop()
    __registrar_state.join()
    __registrar_state = None


class RegistrarWorker(threading.Thread):
    """
    Worker thread for waiting for transactions to go through.
    """
    def __init__(self, config_path):
        super(RegistrarWorker, self).__init__()

        self.config_path = config_path
        config = get_config(config_path)
        self.queue_path = config['queue_path']
        self.poll_interval = int(config['poll_interval'])
        self.api_port = int(config['api_endpoint_port'])
        self.running = True
        self.lockfile_path = None
        self.required_storage_drivers = config.get('storage_drivers_required_write', None)
        if self.required_storage_drivers is None:
            self.required_storage_drivers = config.get("storage_drivers", "").split(",")
        else:
            self.required_storage_drivers = self.required_storage_drivers.split(",")

        log.debug("Queue path:      %s" % self.queue_path)

        if os.path.exists(self.queue_path):
            queue_info = os.stat(self.queue_path)
            log.debug("Queue info:      size=%s ctime=%s atime=%s mtime=%s" % (queue_info.st_size, queue_info.st_ctime, queue_info.st_atime, queue_info.st_mtime))

        else:
            log.debug("Queue info:      <unknown>")

        log.debug("Poll interval:   %s" % self.poll_interval)
        log.debug("API port:        %s" % self.api_port)
        log.debug("Storage:         %s" % ",".join(self.required_storage_drivers))


    @classmethod 
    def register_preordered_name( cls, name_data, payment_privkey_info, owner_privkey_info, proxy=None, config_path=CONFIG_PATH, queue_path=DEFAULT_QUEUE_PATH ):
        """
        Given a preordered name, go register it.
        Return the result of broadcasting the registration operation on success (idempotent--if already broadcasted, then return the broadcast information).
        Return {'error': ...} on error
        Return {'error': ..., 'already_registered': True} if the name is already registered
        Return {'error': ..., 'not_preordered': True} if the name was not preordered
        """
        if proxy is None:
            proxy = get_default_proxy(config_path=config_path)

        if not is_name_registered( name_data['fqu'], proxy=proxy ):
            if in_queue( "preorder", name_data['fqu'], path=queue_path ):
                if not in_queue("register", name_data['fqu'], path=queue_path):
                    # was preordered but not registered
                    # send the registration
                    log.debug('Send async register for {}'.format(name_data['fqu']))
                    log.debug("async_register({}, zonefile={}, token_file={}, transfer_address={})".format(name_data['fqu'], name_data.get('zonefile'), name_data.get('token_file'), name_data.get('transfer_address'))) 
                    res = async_register( name_data['fqu'], payment_privkey_info, owner_privkey_info, name_data=name_data,
                                          proxy=proxy, config_path=config_path, queue_path=queue_path )
                    return res
                else:
                    # already queued 
                    reg_result = queuedb_find( "register", name_data['fqu'], limit=1, path=queue_path )
                    if len(reg_result) == 1:
                        log.debug('Already queued for register: {}'.format(name_data['fqu']))
                        return {'status': True, 'transaction_hash': reg_result[0]['tx_hash']}
                    else:
                        raise Exception("Inconsistency: name '%s' is queued and then unqueued" % name_data['fqu'])

            else:
                log.error('Not preordered: {}'.format(name_data['fqu']))
                return {'error': 'Name "%s" is not preorded' % name_data['fqu'], 'not_preordered': True}

        else:
            log.error('Already registered: {}'.format(name_data['fqu']))
            return {'error': 'Name "%s" is already registered' % name_data['fqu'], 'already_registered': True}

    
    @classmethod
    def set_zonefile( cls, name_data, proxy=None, config_path=CONFIG_PATH, queue_path=DEFAULT_QUEUE_PATH ):
        """
        Given a newly-registered name, go broadcast the hash of its zonefile.
        Idempotent--if the name is already migrated, then return the result of the pending transaction.

        Return {'status': True, 'transaction_hash': ..., 'zonefile_hash': ...} on success
        Return {'error': ...} on error
        """
        if proxy is None:
            proxy = get_default_proxy(config_path=config_path)
    
        conf = get_config(config_path)
        assert conf

        if in_queue('update', name_data['fqu'], path=queue_path):
            # already processed 
            up_result = queuedb_find( "update", name_data['fqu'], limit=1, path=queue_path )
            if len(up_result) == 1:
                return {'status': True, 'transaction_hash': up_result[0]['tx_hash'], 'zonefile_hash': up_result[0].get('zonefile_hash', None)}

            else:
                raise Exception("Queue inconsistency: name '%s' is and is not pending update" % up_result['fqu'])

        log.debug("update({}, zonefile={}, token_file={}, transfer_address={})".format(name_data['fqu'], name_data.get('zonefile'), name_data.get('token_file'), name_data.get('transfer_address'))) 
        res = update( name_data['fqu'], name_data.get('zonefile'), name_data.get('token_file'),
                      name_data.get('zonefile_hash'), name_data.get('transfer_address'), config_path=config_path, proxy=proxy )

        assert 'success' in res

        if not res['success']:
            log.error("migrate %s: %s" % (name_data['fqu'], res['error']))
            return {'error': res['error']}

        else:
            try:
                assert 'transaction_hash' in res
                assert 'value_hash' in res
            except:
                raise Exception("Invalid response\n%s\n" % json.dumps(res, indent=4, sort_keys=True))

            return {'status': True, 'transaction_hash': res['transaction_hash'], 'zonefile_hash': res['value_hash']}


    @classmethod
    def set_zonefiles( cls, queue_path, config_path=CONFIG_PATH, proxy=None ):
        """
        Find all confirmed registrations, create empty zonefiles for them and broadcast their hashes to the blockchain.
        Queue up the zonefiles and token files for subsequent replication.
        Return {'status': True} on success
        Return {'error': ...} on failure
        """
        if proxy is None:
            proxy = get_default_proxy(config_path=config_path)

        ret = {'status': True}
        registers = cls.get_confirmed_registers( config_path, queue_path )
        for register in registers:

            # already migrated?
            if in_queue("update", register['fqu'], path=queue_path):
                log.warn("Already initialized token file for name '%s'" % register['fqu'])
                queue_removeall( [register], path=queue_path )
                continue

            log.debug("Register for '%s' (%s) is confirmed!" % (register['fqu'], register['tx_hash']))
            res = cls.set_zonefile( register, proxy=proxy, queue_path=queue_path, config_path=config_path )
            if 'error' in res:
                log.error("Failed to make name token file for %s: %s" % (register['fqu'], res['error']))
                ret = {'error': 'Failed to set up name token file'}

            else:
                # success!
                log.debug("Sent update for '%s'" % register['fqu'])
                queue_removeall( [register], path=queue_path )

        return ret


    @classmethod
    def get_confirmed_registers( cls, config_path, queue_path ):
        """
        Find all the confirmed registers
        """
        accepted = queue_find_accepted( "register", path=queue_path, config_path=config_path )
        return accepted


    @classmethod
    def get_confirmed_preorders( cls, config_path, queue_path ):
        """
        Find all the confirmed preorders
        """
        accepted = queue_find_accepted( "preorder", path=queue_path, config_path=config_path )
        return accepted


    @classmethod 
    def get_confirmed_updates( cls, config_path, queue_path ):
        """
        Find all confirmed updates
        """
        accepted = queue_find_accepted( "update", path=queue_path, config_path=config_path )
        return accepted


    @classmethod
    def get_confirmed_transfers( cls, config_path, queue_path ):
        """
        Find all confirmed transfers
        """
        accepted = queue_find_accepted( "transfer", path=queue_path, config_path=config_path )
        return accepted


    @classmethod
    def get_confirmed_name_imports( cls, config_path, queue_path ):
        """
        Find all confirmed name imports
        """
        accepted = queue_find_accepted( "name_import", path=queue_path, config_path=config_path )
        return accepted


    @classmethod 
    def register_preorders( cls, queue_path, wallet_data, config_path=CONFIG_PATH, proxy=None ):
        """
        Find all confirmed preorders, and register them.
        Return {'status': True} on success
        Return {'error': ...} on error
        'names' maps to the list of queued name data for names that were registered
        """

        if proxy is None:
            proxy = get_default_proxy(config_path=config_path)

        ret = {'status': True}
        preorders = cls.get_confirmed_preorders( config_path, queue_path )
        for preorder in preorders:

            log.debug("Preorder for '%s' (%s) is confirmed!" % (preorder['fqu'], preorder['tx_hash']))
            
            # did we already register?
            if in_queue("register", preorder['fqu'], path=queue_path):
                log.warn("Already queued name '%s' for registration" % preorder['fqu'])
                queue_removeall( [preorder], path=queue_path )
                continue

            res = cls.register_preordered_name( preorder, wallet_data['payment_privkey'], wallet_data['owner_privkey'], proxy=proxy, config_path=config_path, queue_path=queue_path )
            if 'error' in res:
                if res.get('already_registered'):
                    # can clear out, this is a dup
                    log.debug("%s is already registered!" % preorder['fqu'])
                    queue_removeall( [preorder], path=queue_path )

                else:
                    log.error("Failed to register preordered name %s: %s" % (preorder['fqu'], res['error']))
                    ret = {'error': 'Failed to preorder a name'} 

            else:
                # clear 
                log.debug("Sent register for %s" % preorder['fqu'] )
                queue_removeall( [preorder], path=queue_path )

        return ret


    @classmethod
    def clear_confirmed( cls, config_path, queue_path, proxy=None ):
        """
        Find all confirmed transactions besides preorder, register, update, and remove them from the queue.
        Once these operations complete, there will be no subsequent operations.
        
        Return {'status': true} on success
        Return {'error': ...} on failure
        """
        for queue_name in ['transfer', 'revoke', 'renew', 'name_import']:
            accepted = queue_find_accepted( queue_name, path=queue_path, config_path=config_path )

            if len(accepted) > 0:
                log.debug("Clear %s confirmed %s operations" % (len(accepted), queue_name))
                queue_removeall( accepted, path=queue_path )

        # remove expired preorders
        cleanup_preorder_queue(path=queue_path, config_path=config_path)
        return {'status': True}

    
    @classmethod 
    def replicate_name_data( cls, name_data, atlas_servers, wallet_data, storage_drivers, config_path, proxy=None, replicated_zonefiles=[], replicated_token_file_hashes=[] ):
        """
        Given an update queue entry,
        replicate the zonefile to as many
        blockstack atlas servers as we can.
        If given, replicate the token file as well.
        @atlas_servers should be a list of (host, port)
        Return {'status': True} on success
        Return {'error': ...} on error
        """
     
        # is the zonefile hash replicated?
        zonefile_data = name_data['zonefile']
        if zonefile_data is None:
            log.debug("No zonefile to replicate for %s" % name_data['fqu'])
            return {'status': True}

        zonefile_hash = name_data.get('zonefile_hash', None)
        if zonefile_hash is None:
            zonefile_hash = get_zonefile_data_hash( zonefile_data )

        if zonefile_hash not in replicated_zonefiles:
            # NOTE: replicated_zonefiles is static but scoped to this method
            # use it to remember what we've replicated, so we don't needlessly retry
            name_rec = get_name_blockchain_record( name_data['fqu'], proxy=proxy )
            if 'error' in name_rec:
                return name_rec

            if BLOCKSTACK_TEST:
                log.debug("Replicate zonefile %s (blockchain: %s)\ndata:\n%s" % (zonefile_hash, name_rec['value_hash'], base64.b64encode(zonefile_data)))

            if str(name_rec['value_hash']) != zonefile_hash:
                log.error("Zonefile %s has not been confirmed yet (still on %s)" % (zonefile_hash, name_rec['value_hash']))
                return {'error': 'Zonefile hash not yet replicated'}

            res = zonefile_data_replicate( name_data['fqu'], zonefile_data, name_data['tx_hash'], atlas_servers, config_path=config_path, storage_drivers=storage_drivers )
            if 'error' in res:
                log.error("Failed to replicate zonefile %s for %s: %s" % (zonefile_hash, name_data['fqu'], res['error']))
                return res

            log.info("Replicated zonefile data for %s to %s server(s)" % (name_data['fqu'], len(res['servers'])))
            replicated_zonefiles.append(zonefile_hash)

        # replicate token file to storage, if given
        if name_data.has_key('token_file') and name_data['token_file'] is not None:
            # only works this is actually a zonefile, since we need to use
            # the zonefile to find the appropriate data private key.
            zonefile = None
            try:
                zonefile = blockstack_zones.parse_zone_file( zonefile_data )
                assert is_user_zonefile( zonefile )
            except Exception, e:
                if BLOCKSTACK_TEST:
                    log.exception(e)

                log.warning("Not a standard zone file; not replicating token file for %s" % name_data['fqu'])
                return {'status': True}
           
            signing_privkey = get_signing_privkey( get_owner_privkey_info(wallet_data) )
            assert signing_privkey

            log.info("Replicate token file data for %s to %s" % (name_data['fqu'], ",".join(storage_drivers)))
            
            token_file_payload = copy.deepcopy(name_data['token_file'])
            token_file_hash = hashlib.sha256(name_data['fqu'] + zonefile_hash + token_file_payload).hexdigest()

            # did we replicate this token file for this name and zonefile already?
            if token_file_hash in replicated_token_file_hashes:
                # already replicated
                log.debug("Already replicated token file for {}".format(name_data['fqu']))
                return {'status': True}
            
            res = token_file_put(name_data['fqu'], token_file_payload, signing_privkey, required_drivers=storage_drivers, config_path=config_path)
            if 'error' in res:
                log.error("Failed to replicate token file for {}: {}".format(name_data['fqu'], res['error']))
                return {'error': "Failed to replicate token file"}

            log.info("Replicated token file for %s" % (name_data['fqu']))

            # don't do this again 
            replicated_token_file_hashes.append(token_file_hash)
            return {'status': True}

        else:
            log.info("No token file to replicate for '%s'" % (name_data['fqu']))
            return {'status': True}


    @classmethod
    def replicate_names_data( cls, queue_path, updates, wallet_data, storage_drivers, skip=[], config_path=CONFIG_PATH, proxy=None ):
        """
        Replicate all zonefiles and token files for each confirmed update or name import.
        @atlas_servers should be a list of (host, port)

        Do NOT remove items from the queue.

        Return {'status': True} on success
        Return {'error': ..., 'names': [...]} on failure.  'names' refers to the list of names that failed
        """
        ret = {'status': True} 
        failed_names = []

        atlas_servers = cls.get_atlas_server_list( config_path )
        if 'error' in atlas_servers:
            log.warn('Failed to get server list: {}'.format(servers['error']))
            return {'error': 'Failed to get Atlas server list'}

        for update in updates:
            if update['fqu'] in skip:
                log.debug("Skipping name {}".format(update['fqu']))
                continue

            log.debug("Zone file update on '%s' (%s) is confirmed!  New hash is %s" % (update['fqu'], update['tx_hash'], update['zonefile_hash']))
            res = cls.replicate_name_data( update, atlas_servers, wallet_data, storage_drivers, config_path, proxy=proxy )
            if 'error' in res:
                log.error("Failed to update %s: %s" % (update['fqu'], res['error']))
                ret = {'error': 'Failed to finish an update'}
                failed_names.append( update['fqu'] )
                
        if 'error' in ret:
            ret['names'] = failed_names

        return ret
      

    @classmethod
    def replicate_update_data( cls, queue_path, wallet_data, storage_drivers, skip=[], config_path=CONFIG_PATH, proxy=None ):
        """
        Replicate all zone files and token files for each confirmed NAME_UPDATE
        @atlas_servers should be a list of (host, port)

        Do NOT remove items from the queue.

        Return {'status': True} on success
        Return {'error': ..., 'names': [...]} on failure.  'names' refers to the list of names that failed
        """
        updates = cls.get_confirmed_updates( config_path, queue_path )
        if len(updates) == 0:
            return {'status': True}

        return cls.replicate_names_data(queue_path, updates, wallet_data, storage_drivers, skip=skip, config_path=config_path, proxy=proxy)


    @classmethod
    def replicate_name_import_data( cls, queue_path, wallet_data, storage_drivers, skip=[], config_path=CONFIG_PATH, proxy=None ):
        """
        Replicate all zone files and token files for each confirmed NAME_UPDATE
        @atlas_servers should be a list of (host, port)

        Do NOT remove items from the queue.

        Return {'status': True} on success
        Return {'error': ..., 'names': [...]} on failure.  'names' refers to the list of names that failed
        """
        name_imports = cls.get_confirmed_name_imports( config_path, queue_path )
        if len(name_imports) == 0:
            return {'status': True}

        return cls.replicate_names_data(queue_path, name_imports, wallet_data, storage_drivers, skip=skip, config_path=config_path, proxy=proxy)


    @classmethod
    def transfer_names( cls, queue_path, skip=[], config_path=CONFIG_PATH, proxy=None ):
        """
        Find all confirmed updates and if they have a transfer address, transfer them.
        Otherwise, clear them from the update queue.

        Return {'status': True} on success
        Return {'error': ..., 'names': ...} on failure
        """
        if proxy is None:
            proxy = get_default_proxy(config_path=config_path)
    
        failed = []
        ret = {'status': True}
        conf = get_config(config_path)
        assert conf

        updates = cls.get_confirmed_updates( config_path, queue_path )
        for update in updates:
            if update['fqu'] in skip:
                log.debug("Skipping {}".format(update['fqu']))
                continue

            if update.get("transfer_address") is not None:
                # let's see if the name already got there!
                name_rec = get_name_blockchain_record( update['fqu'], proxy=proxy )
                if 'address' in name_rec and name_rec['address'] == update['transfer_address']:
                    log.debug("Requested Transfer {} to {} is owned by {} already. Declaring victory.".format(
                        update['fqu'], update['transfer_address'], name_rec['address']))
                    res = { 'success' : True }
                else:
                    log.debug("Transfer {} to {}".format(update['fqu'], update['transfer_address']))

                    res = transfer( update['fqu'], update['transfer_address'], config_path=config_path, proxy=proxy )

                assert 'success' in res

                if res['success']:
                    # clear from update queue
                    log.debug("Clearing successful transfer of {} to {} from update queue".format(update['fqu'], update['transfer_address']))
                    queue_removeall( [update], path=queue_path )

                else:
                    # will try again 
                    log.error("Failed to transfer {} to {}: {}".format(update['fqu'], update['transfer_address'], res.get('error')))
                    queue_add_error_msg('update', update['fqu'], res.get('error'), path=queue_path)
                    ret = {'error': 'Not all names transferred'}
                    failed.append(update['fqu'])

            else:
                # nothing more to do 
                log.debug("Done working on {}".format(update['fqu']))
                log.debug("Final name output: {}".format(update))
                queue_removeall( [update], path=queue_path )

        if 'error' in ret:
            ret['names'] = failed

        return ret


    @classmethod 
    def get_atlas_server_list( cls, config_path ):
        """
        Get the list of atlas servers to which to replicate zonefiles
        Returns [(host, port)] on success
        Returns {'error': ...} on error
        """
        conf = get_config(config_path)
        servers = ['{}:{}'.format(conf['server'], conf['port'])]
        server_hostport = '{}:{}'.format(conf['server'], conf['port'])

        atlas_peers_res = {}
        try:
            atlas_peers_res = get_atlas_peers( server_hostport )
            assert 'error' not in atlas_peers_res
           
            servers += atlas_peers_res['peers']

        except AssertionError as ae:
            log.exception(ae)
            log.error('Error response from {}: {}'.format(server_hostport, atlas_peers_res['error']))
            return {'error': 'Failed to get valid response'}
        except socket.error, se:
            log.exception(se)
            log.warning('Failed to find Atlas peers of {}'.format(server_hostport))
            return {'error': 'Failed to get atlas peers due to socket error'}
        except Exception as e:
            log.exception(e)
            return {'error': 'Failed to contact atlas peer'}
            
        servers = list(set([str(hp) for hp in servers]))

        if 'node.blockstack.org:6264' not in servers and not BLOCKSTACK_TEST:
            log.warning("Also including node.blockstack.org:6264 for Atlas zone file dissimination")
            servers.append("node.blockstack.org:6264")

        log.debug("Servers: {}".format(servers))

        return [url_to_host_port(hp) for hp in servers]


    def cleanup_lockfile(self, path):
        """
        Remove a lockfile (exit handler)
        """
        if self.lockfile_path is None:
            return

        try:
            os.unlink(self.lockfile_path)
            self.lockfile_path = None
        except:
            pass


    def request_stop(self):
        """
        Stop this thread
        """
        self.running = False


    @classmethod
    def is_lockfile_stale( cls, path ):
        """
        Is the given lockfile stale?
        """
    
        with open(path, "r") as f:
            dat = f.read()
            try:
                pid = int(dat.strip())
            except:
                # corrupt
                pid = -1

        return pid != os.getpid()


    @classmethod
    def lockfile_write( cls, fd ):
        """
        Put a lockfile
        Return True on success
        Return False on error
        """
        
        buf = "%s\n" % os.getpid()
        nw = 0
        while nw < len(buf):
            try:
                rc = os.write( fd, buf[nw:] )
                nw += rc
            except:
                log.error("Failed to write lockfile")
                return False

        return True


    @classmethod
    def get_lockfile_path( cls, config_path ):
        """
        Get the path to the lockfile
        """
        return os.path.join( os.path.dirname(config_path), "registrar.lock" )


    @classmethod 
    def is_lockfile_valid( cls, config_path ):
        """
        Does the lockfile exist and does it correspond
        to a running registrar?
        """
        lockfile_path = cls.get_lockfile_path( config_path )
        if os.path.exists(lockfile_path):
            # is it stale?
            if cls.is_lockfile_stale( lockfile_path ):
                return False

            else:
                # not stale
                return True

        else:
            return False


    def run(self):
        """
        Watch the various queues:
        * if we find an accepted preorder, send the accompanying register
        * if we find an accepted update, replicate the accompanying zonefile
        """
        failed = False
        poll_interval = self.poll_interval
        failed_names = []

        log.info("Registrar worker entered")

        # set up a lockfile
        self.lockfile_path = RegistrarWorker.get_lockfile_path( self.config_path )
        if not RegistrarWorker.is_lockfile_valid( self.config_path ):
            if os.path.exists(self.lockfile_path):
                log.debug("Removing stale lockfile")
                os.unlink(self.lockfile_path)

        else:
            log.debug("Extra worker thread exiting (lockfile exists and is valid)")
            return

        try:
            fd, path = tempfile.mkstemp(prefix=".registrar.lock.", dir=os.path.dirname(self.config_path))
            os.link( path, self.lockfile_path )
            
            try:
                os.unlink(path)
            except:
                pass

            # success!  write the lockfile
            rc = RegistrarWorker.lockfile_write( fd )
            os.close( fd )

            if not rc:
                log.error("Failed to write lockfile")
                return

        except (IOError, OSError):
            try:
                os.unlink(path)
            except:
                pass

            log.debug("Extra worker exiting (failed to lock)")
            return

        log.debug("Registrar worker starting up")

        while self.running:

            failed = False
            wallet_data = None
            proxy = get_default_proxy( config_path=self.config_path )
            failed_names = []

            try:
                wallet_data = get_wallet( config_path=self.config_path, proxy=proxy )

                # wait until the owner address is set 
                while ('error' in wallet_data or wallet_data['owner_address'] is None) and self.running:
                    log.debug("Owner address not set... (%s)" % wallet_data.get("error", ""))
                    wallet_data = get_wallet( config_path=self.config_path, proxy=proxy )
                    time.sleep(1.0)
                
                # preemption point
                if not self.running:
                    break

            except Exception, e:
                log.exception(e)
                break
                poll_interval = 1.0

            try:
                # see if we can complete any registrations
                # clear out any confirmed preorders
                log.debug("register all pending preorders in %s" % (self.queue_path))
                res = RegistrarWorker.register_preorders( self.queue_path, wallet_data, config_path=self.config_path, proxy=proxy )
                if 'error' in res:
                    log.warn("Registration failed: %s" % res['error'])

                    # try exponential backoff
                    failed = True
                    poll_interval = 1.0

            except Exception, e:
                log.exception(e)
                failed = True
                poll_interval = 1.0

            try:
                # see if we can put any zonefiles
                # clear out any confirmed registers
                log.debug("put zonefile hashes for registered names in %s" % (self.queue_path))
                res = RegistrarWorker.set_zonefiles( self.queue_path, config_path=self.config_path, proxy=proxy )
                if 'error' in res:
                    log.warn('zonefile hash broadcast failed: %s' % res['error'])

                    # try exponential backoff 
                    failed = True
                    poll_interval = 1.0

            except Exception, e:
                log.exception(e)
                failed = True
                poll_interval = 1.0

            try:
                # see if we can replicate any zonefiles and token files
                # clear out any confirmed updates
                log.debug("replicate all pending zone files and token files for updates %s" % (self.queue_path))
                res = RegistrarWorker.replicate_update_data( self.queue_path, wallet_data, self.required_storage_drivers, config_path=self.config_path, proxy=proxy )
                if 'error' in res:
                    log.warn("Zone file/token file replication failed for update: %s" % res['error'])

                    # try exponential backoff
                    failed = True
                    poll_interval = 1.0
                    failed_names += res['names']

            except Exception, e:
                log.exception(e)
                failed = True
                poll_interval = 1.0

            try:
                # see if we can transfer any names to their new owners
                log.debug("transfer all names in {}".format(self.queue_path))
                res = RegistrarWorker.transfer_names( self.queue_path, skip=failed_names, config_path=self.config_path, proxy=proxy )
                if 'error' in res:
                    log.warn("Transfer failed: {}".format(res['error']))

                    # try exponential backoff
                    failed = True
                    poll_interval = 1.0
                    failed_names += res['names']

            except Exception as e:
                log.exception(e)
                failed = True
                poll_interval = 1.0

            try:
                # see if we can replicate any zonefiles for name imports
                # clear out any confirmed imports
                log.debug("replicate all pending zone files for name imports in {}".format(self.queue_path))
                res = RegistrarWorker.replicate_name_import_data( self.queue_path, wallet_data, self.required_storage_drivers, skip=failed_names, config_path=self.config_path, proxy=proxy )
                if 'error' in res:
                    log.warn("Zone file replication failed: {}".format(res['error']))

                    # try exponential backoff
                    failed = True
                    poll_interval = 1.0
                    failed_names += res['names']

            except Exception, e:
                log.exception(e)
                failed = True
                poll_interval = 1.0

            try:
                # see if we can remove any other confirmed operations, besides preorders, registers, and updates
                log.debug("clean out other confirmed operations")
                res = RegistrarWorker.clear_confirmed( self.config_path, self.queue_path, proxy=proxy )
                if 'error' in res:
                    log.warn("Failed to clear out some operations: %s" % res['error'])

                    # try exponential backoff
                    failed = True
                    poll_interval = 1.0

            except Exception, e:
                log.exception(e)
                failed = True
                poll_interval = 1.0

            # if we failed a step, then try again quickly with exponential backoff
            if failed:
                poll_interval = 2 * poll_interval + random.random() * poll_interval

            else:
                # succeeded. resume normal polling 
                poll_interval = self.poll_interval
           
            try:
                log.debug("Sleep for %s" % poll_interval)
                for i in xrange(0, int(poll_interval)):
                    time.sleep(1)

                    # preemption point
                    if not self.running:
                        break

            except:
                # interrupted
                log.debug("Sleep interrupted")
                break

        log.info("Registrar worker exited")
        self.cleanup_lockfile( self.lockfile_path )


class RegistrarState(object):
    """
    State bundle for the RPC calls
    """
    finished = False

    payment_address = None
    owner_address = None

    payment_privkey_info = None
    owner_privkey_info = None
    data_privkey_info = None
    data_pubkey = None

    server_started_at = None
    registrar_worker = None
    queue_path = None

    def __init__(self, config_path):

        self.config_path = config_path
        conf = get_config(config_path)
        self.queue_path = conf['queue_path']
        log.info("Registrar initialized (config: %s, queues: %s)" % (config_path, self.queue_path))
        self.registrar_worker = RegistrarWorker( config_path )


    def start(self):
        self.registrar_worker.start()

    def request_stop(self):
        log.debug("Registrar worker request stop")
        self.registrar_worker.request_stop()

    def join(self):
        log.debug("Registrar worker join")
        self.registrar_worker.join()


# RPC method: backend_state
def state():
    """
    Return status on current registrations
    """
    state, config_path, proxy = get_registrar_state()

    log.debug("Get queue state from %s" % state.queue_path)
    data = get_queue_state(path=state.queue_path)
    return data


# RPC method: backend_set_wallet
def set_wallet(payment_keypair, owner_keypair, data_keypair, config_path=None, proxy=None):
    """
    Keeps payment privkey in memory (instead of disk)
    for the time that server is alive.

    Each _keypair is a list or tuple with two items: the address, and the private key information
    (note that the private key information can be either a private key, or a multisig info dict).

    Return {'success': True} on success
    Return {'error': ...} on error
    """
    state, config_path, proxy = get_registrar_state(config_path=config_path, proxy=proxy)

    try:
        assert payment_keypair[0]
        assert payment_keypair[1]
        assert owner_keypair[0]
        assert owner_keypair[1]
        assert data_keypair[0]
        assert data_keypair[1]
    except AssertionError as ae:
        if BLOCKSTACK_TEST or BLOCKSTACK_DEBUG:
            log.exception(ae)

        return {'error': 'Missing wallet information'}

    # sanity check...
    if not virtualchain.is_singlesig( payment_keypair[1] ) and not virtualchain.is_multisig( payment_keypair[1] ):
        return {'error': 'Invalid payment key info'}

    if not virtualchain.is_singlesig( owner_keypair[1] ) and not virtualchain.is_multisig( owner_keypair[1] ):
        return {'error': 'Invalid owner key info'}

    if not is_singlesig_hex( data_keypair[1] ):
        return {'error': 'Invalid data key info'}

    state.payment_address = payment_keypair[0]
    state.owner_address = owner_keypair[0]
    state.data_pubkey = ECPrivateKey(data_keypair[1]).public_key().to_hex()

    if keylib.key_formatting.get_pubkey_format(state.data_pubkey) == 'hex_compressed':
        state.data_pubkey = keylib.key_formatting.decompress(state.data_pubkey)

    state.payment_privkey_info = payment_keypair[1]
    state.owner_privkey_info = owner_keypair[1]
    state.data_privkey_info = data_keypair[1]

    data = {}
    data['success'] = True

    log.debug("Wallet set (%s, %s, %s)" % (state.payment_address, state.owner_address, data_keypair[0]))
    return data


def get_wallet_payment_privkey_info(config_path=None, proxy=None):
    """
    Get the decrypted payment private key info from the wallet
    Return None if not set
    """
    state, config_path, proxy = get_registrar_state(config_path=config_path, proxy=proxy)
    if state.payment_privkey_info is None:
        return None

    return state.payment_privkey_info


def get_wallet_owner_privkey_info(config_path=None, proxy=None):
    """
    Get the decrypted owner private key info from the wallet
    Return None if not set
    """
    state, config_path, proxy = get_registrar_state(config_path=config_path, proxy=proxy)
    if state.owner_privkey_info is None:
        return None

    return state.owner_privkey_info


def get_wallet_data_privkey_info(config_path=None, proxy=None):
    """
    Get the decrypted data private key info from the wallet
    Return None if not set
    """
    state, config_path, proxy = get_registrar_state(config_path=config_path, proxy=proxy)
    if state.data_privkey_info is None:
        return None 

    return state.data_privkey_info


# RPC method: backend_get_wallet
def get_wallet(config_path=None, proxy=None):
    """
    Keeps payment privkey in memory (instead of disk)
    for the time that server is alive
    Return the wallet (as a JSON dict) on success
    Return {'error':...} on error
    """

    state, config_path, proxy = get_registrar_state(config_path=config_path, proxy=proxy)
    data = {}
    
    data['payment_address'] = state.payment_address
    data['owner_address'] = state.owner_address
    data['data_pubkey'] = state.data_pubkey

    data['payment_privkey'] = get_wallet_payment_privkey_info(config_path=config_path, proxy=proxy)
    data['owner_privkey'] = get_wallet_owner_privkey_info(config_path=config_path, proxy=proxy)
    data['data_privkey'] = get_wallet_data_privkey_info(config_path=config_path, proxy=proxy)

    if data['payment_privkey'] is None or data['owner_privkey'] is None or data['data_privkey'] is None:
        if data['payment_privkey'] is None:
            log.debug("No payment private key(s)")

        if data['owner_privkey'] is None:
            log.debug("No owner private key(s)")

        if data['data_privkey'] is None:
            log.debug("No data private key(s)")

        data['error'] = "Failed to load private keys (wrong password?)"

    return data


def preorder(fqu, cost_satoshis, zonefile_data, token_file, transfer_address, min_payment_confs, proxy=None, config_path=CONFIG_PATH):
    """
    Send preorder transaction and enter it in queue.
    Queue up additional state so we can update and transfer it as well.
    The entered registration is picked up
    by the monitor process.
    Return {'success': True, ...} on success
    Return {'error': ...} on error
    """

    state, config_path, proxy = get_registrar_state(config_path=config_path, proxy=proxy)
    data = {}

    if min_payment_confs is None:
        min_payment_confs = TX_MIN_CONFIRMATIONS
    else:
        log.warn("Using {} confirmations instead of the default {}".format(min_payment_confs, TX_MIN_CONFIRMATIONS))

    if state.payment_address is None or state.owner_address is None:
        log.debug("Wallet is not unlocked")
        data['success'] = False
        data['error'] = "Wallet is not unlocked."
        return data

    if in_queue("preorder", fqu, path=state.queue_path):
        log.debug("Already enqueued: %s" % fqu)
        data['success'] = False
        data['error'] = "Already in queue."
        return data

    payment_privkey_info = get_wallet_payment_privkey_info(config_path=config_path, proxy=proxy)
    owner_privkey_info = get_wallet_owner_privkey_info(config_path=config_path, proxy=proxy)

    name_data = {
        'transfer_address': transfer_address,
        'zonefile': zonefile_data,
        'token_file': token_file,
    }
    
    log.debug("async_preorder({}, zonefile_data={}, token_file={}, transfer_address={})".format(fqu, zonefile_data, token_file, transfer_address)) 
    resp = async_preorder(fqu, payment_privkey_info, owner_privkey_info, cost_satoshis,
                          name_data=name_data, min_payment_confs=min_payment_confs,
                          proxy=proxy, config_path=config_path, queue_path=state.queue_path)

    if 'error' not in resp:
        data['success'] = True
        data['message'] = "The name has been queued up for registration and"
        data['message'] += " will take a few hours to go through. You can"
        data['message'] += " check on the status at any time by running"
        data['message'] += " 'blockstack info'."
        data['transaction_hash'] = resp['transaction_hash']
    else:
        data['success'] = False
        data['message'] = "Couldn't broadcast transaction. You can try again."
        data['error'] = resp['error']

    if 'tx' in resp:
        data['tx'] = resp['tx']

    return data


def update( fqu, zonefile_txt, token_file, zonefile_hash, transfer_address, config_path=CONFIG_PATH, proxy=None ):
    """
    Send a new zonefile hash.  Queue the zonefile data for subsequent replication.
    zonefile_txt_b64 must be b64-encoded so we can send it over RPC sanely
    """

    state, config_path, proxy = get_registrar_state(config_path=config_path, proxy=proxy)
    data = {}

    assert zonefile_txt is not None or zonefile_hash is not None, "need zonefile or zonefile hash"
    
    if zonefile_hash is None:
        zonefile_hash = get_zonefile_data_hash( zonefile_txt )
        
    if state.payment_address is None or state.owner_address is None:
        data['success'] = False
        data['error'] = "Wallet is not unlocked."
        return data

    if in_queue("update", fqu, path=state.queue_path):
        data['success'] = False
        data['error'] = "Already in queue."
        return data

    resp = None

    payment_privkey_info = get_wallet_payment_privkey_info(config_path=config_path, proxy=proxy)
    owner_privkey_info = get_wallet_owner_privkey_info(config_path=config_path, proxy=proxy)

    replication_error = None

    if not is_zonefile_hash_current(fqu, zonefile_hash, proxy=proxy ):
        # new zonefile data
        name_data = {
            'transfer_address': transfer_address
        }

        log.debug("async_update({}, zonefile_data={}, token_file={}, transfer_address={})".format(fqu, zonefile_txt, token_file, transfer_address)) 
        resp = async_update(fqu, zonefile_txt, token_file,
                            owner_privkey_info,
                            payment_privkey_info,
                            name_data=name_data,
                            zonefile_hash=zonefile_hash,
                            proxy=proxy,
                            config_path=config_path,
                            queue_path=state.queue_path)

    else:
        return {'success': True, 'warning': "The zonefile has not changed, so no update sent."}

    if 'error' not in resp:

        data['success'] = True
        data['message'] = "The name has been queued up for update and"
        data['message'] += " will take ~1 hour to process. You can"
        data['message'] += " check on the status at any time by running"
        data['message'] += " 'blockstack info'."
        data['transaction_hash'] = resp['transaction_hash']
        data['value_hash'] = resp['zonefile_hash']
    else:
        log.error("async_update failed with: '%s'" % resp['error'])
        data['success'] = False
        data['message'] = "Couldn't broadcast transaction. You can try again."
        data['error'] = resp['error']


    if replication_error is not None:
        data['warning'] = "Failed to replicate the zonefile ('%s')" % replication_error

    if 'tx' in resp:
        data['tx'] = resp['tx']

    return data


# RPC method: backend_transfer
def transfer(fqu, transfer_address, config_path=CONFIG_PATH, proxy=None ):
    """
    Send transfer transaction.
    Keeps the zonefile data.

    Return {'success': True, 'transaction_hash': ..., 'message': ...} on success
    Return {'success': False, 'error': ...}
    """

    state, config_path, proxy = get_registrar_state(config_path=config_path, proxy=proxy)
    data = {}

    if state.payment_address is None or state.owner_address is None:
        data['success'] = False
        data['error'] = "Wallet is not unlocked."
        return data

    if in_queue("transfer", fqu, path=state.queue_path):
        data['success'] = False
        data['error'] = "Already in queue."
        return data

    payment_privkey_info = get_wallet_payment_privkey_info(config_path=config_path, proxy=proxy)
    owner_privkey_info = get_wallet_owner_privkey_info(config_path=config_path, proxy=proxy)

    resp = async_transfer(fqu, transfer_address,
                          owner_privkey_info,
                          payment_privkey_info,
                          proxy=proxy,
                          config_path=config_path,
                          queue_path=state.queue_path)

    if 'error' not in resp:
        data['success'] = True
        data['message'] = "The name has been queued up for transfer and"
        data['message'] += " will take ~1 hour to process. You can"
        data['message'] += " check on the status at any time by running"
        data['message'] += " 'blockstack info'."
        data['transaction_hash'] = resp['transaction_hash']
    else:
        data['success'] = False
        data['error'] = resp['error']

    if 'tx' in resp:
        data['tx'] = resp['tx']

    return data


# RPC method: backend_renew
def renew( fqu, renewal_fee, config_path=CONFIG_PATH, proxy=None ):
    """
    Renew a name

    Return {'success': True, 'message': ..., 'transaction_hash': ...} on success
    Return {'error': ...} on error
    """

    state, config_path, proxy = get_registrar_state(config_path=config_path, proxy=proxy)
    data = {}

    if state.payment_address is None or state.owner_address is None:
        data['success'] = False
        data['error'] = "Wallet is not unlocked."
        return data

    if in_queue("renew", fqu, path=state.queue_path):
        data['success'] = False
        data['error'] = "Already in queue."
        return data

    resp = None

    payment_privkey_info = get_wallet_payment_privkey_info(config_path=config_path, proxy=proxy)
    owner_privkey_info = get_wallet_owner_privkey_info(config_path=config_path, proxy=proxy)

    resp = async_renew(fqu, owner_privkey_info, payment_privkey_info, renewal_fee,
                       proxy=proxy,
                       config_path=config_path,
                       queue_path=state.queue_path)

    if 'error' not in resp:

        data['success'] = True
        data['message'] = "The name has been queued up for renewal and"
        data['message'] += " will take ~1 hour to process. You can"
        data['message'] += " check on the status at any time by running"
        data['message'] += " 'blockstack info'."
        data['transaction_hash'] = resp['transaction_hash']
    else:
        data['success'] = False
        data['message'] = "Couldn't broadcast transaction. You can try again."
        data['error'] = resp['error']

    if 'tx' in resp:
        data['tx'] = resp['tx']

    return data


# RPC method: backend_revoke
def revoke( fqu, config_path=CONFIG_PATH, proxy=None ):
    """
    Revoke a name

    Return {'success': True, 'message': ..., 'transaction_hash': ...} on success
    Return {'error': ...} on error
    """

    state, config_path, proxy = get_registrar_state(config_path=config_path, proxy=proxy)
    data = {}

    if state.payment_address is None or state.owner_address is None:
        data['success'] = False
        data['error'] = "Wallet is not unlocked."
        return data

    if in_queue("revoke", fqu, path=state.queue_path):
        data['success'] = False
        data['error'] = "Already in queue."
        return data

    resp = None

    payment_privkey_info = get_wallet_payment_privkey_info(config_path=config_path, proxy=proxy)
    owner_privkey_info = get_wallet_owner_privkey_info(config_path=config_path, proxy=proxy)

    resp = async_revoke(fqu, owner_privkey_info, payment_privkey_info,
                        proxy=proxy,
                        config_path=config_path,
                        queue_path=state.queue_path)

    if 'error' not in resp:

        data['success'] = True
        data['message'] = "The name has been queued up for revocation and"
        data['message'] += " will take ~1 hour to process. You can"
        data['message'] += " check on the status at any time by running"
        data['message'] += " 'blockstack info'."
        data['transaction_hash'] = resp['transaction_hash']
    else:
        data['success'] = False
        data['message'] = "Couldn't broadcast transaction. You can try again."
        data['error'] = resp['error']

    if 'tx' in resp:
        data['tx'] = resp['tx']

    return data

