#!/usr/bin/python3
#-*- encoding: Utf-8 -*-
from subprocess import Popen, run, PIPE, DEVNULL, STDOUT, TimeoutExpired, list2cmdline
from socket import socket, AF_INET, SOCK_STREAM
from os.path import realpath, dirname
from sys import stderr, platform
from logging import debug
from shutil import which
from time import sleep
from re import search

try:
    from os import setpgrp
except ImportError:
    setpgrp = None

from inputs._hdlc_mixin import HdlcMixin
from inputs._base_input import BaseInput

INPUTS_DIR = dirname(realpath(__file__))
ADB_BRIDGE_DIR = realpath(INPUTS_DIR + '/adb_bridge')

# Print shell output to stdout when "-v" is passed to QCSuper

def run_safe(args, **kwargs):
    debug('[>] Running command: ' + list2cmdline(args))
    result = run(args, **kwargs)
    result_string = ((result.stdout or b'') + (result.stderr or b''))
    if result and result_string:
        debug('[<] Obtained result for running "%s": %s' % (list2cmdline(args), result_string))
    return result

"""
    This class implements reading Qualcomm DIAG data from a the /dev/diag
    character device, on the local device.
    
    For this, it uses the C program located in "./adb_bridge/" which
    creates a TCP socket acting as a proxy to /dev/diag.
"""

QCSUPER_TCP_PORT = 43555

class AndroidConnector(HdlcMixin, BaseInput):
    
    def __init__(self):
        self._disposed = False

        self.su_command = '%s'
        
        # Send batch commands to check for the writability of /dev/diag,
        # and for the availability of the "su" command
        
        bash_output = self.adb_shell(
            'test -w /dev/diag; echo DIAG_NOT_WRITEABLE=$?; ' +
            'test -e /dev/diag; echo DIAG_NOT_EXISTS=$?; ' +
            'test -r /dev; echo DEV_NOT_READABLE=$?; ' +
            'su -c id'
        )
        
        # Check for the presence of /dev/diag
        
        if not search('DIAG_NOT_WRITEABLE=[01]', bash_output):
            
            print('Could not run a bash command, is your phone environment set up properly?')
            
            exit(bash_output)
        
        # If writable, continue
        
        elif 'DIAG_NOT_WRITEABLE=0' in bash_output:
            
            pass

        # If not present, raise an error
        
        elif 'DEV_NOT_READABLE=0' in bash_output and 'DIAG_NOT_EXISTS=1' in bash_output:
            
            exit('Could not find /dev/diag, does your phone have a Qualcomm chip?')

        # If maybe present but not writable, check for root
        
        elif 'uid=0' in bash_output:
            
            self.su_command = 'su -c "%s"'
        
        elif 'uid=0' in self.adb_shell('su 0,0 sh -c "id"'):
            
            self.su_command = 'su 0,0 sh -c "%s"'
    
        else:
            exit('Could not get root to adb, is your phone rooted?')
        
        
        # Once root has been obtained, send batch commands to check
        # for the presence of /dev/diag
        
        bash_output = self.adb_shell(
            'test -e /dev/diag; echo DIAG_NOT_EXISTS=$?'
        )
        
        # If not present, raise an error
        
        if 'DIAG_NOT_EXISTS=1' in bash_output:
            
            exit('Could not find /dev/diag, does your phone have a Qualcomm chip?')
        
        # Launch the adb_bridge
        
        self._relaunch_adb_bridge()

        self.packet_buffer = b''
        
        super().__init__()
    
    def _relaunch_adb_bridge(self):
        
        if hasattr(self, 'adb_proc'):
            self.adb_proc.terminate()
        
        self.adb_proc = Popen([ADB_BRIDGE_DIR + '/adb_bridge'],
            
            stdin = DEVNULL, stdout = PIPE, stderr = STDOUT,
            preexec_fn = setpgrp,
            bufsize = 0, universal_newlines = True
        )
    
        for line in self.adb_proc.stdout:
            
            if 'Connection to Diag established' in line:
                
                break
            
            else:
                
                stderr.write(line)
                stderr.flush()

        self.socket = socket(AF_INET, SOCK_STREAM)

        try:
            
            self.socket.connect(('localhost', QCSUPER_TCP_PORT))
        
        except Exception:
            
            # self.adb_proc.terminate()
            
            exit('Could not communicate with the adb_bridge program')
        
        self.received_first_packet = False
    
    """
        This utility function tries to run a command to adb,
        raising an exception when it is unreachable.
        
        :param command: A shell command (string)
        
        :returns The combined stderr and stdout from "adb shell" (string)
    """
    
    def adb_shell(self, command):
        adb = run(['bash', '-c', command], capture_output=True, text=True)

        # return adb.stdout.decode('utf8').strip()
        return adb.stdout.strip()
    
    def send_request(self, packet_type, packet_payload):
        
        raw_payload = self.hdlc_encapsulate(bytes([packet_type]) + packet_payload)
        
        self.socket.send(raw_payload)
    
    def get_gps_location(self):
        
        lat = None
        lng = None
        
        gps_info = run_safe(['dumpsys', 'location'], stdout = PIPE)
        gps_info = gps_info.stdout.decode('utf8')
        
        gps_info = search('(\d+\.\d+),(\d+\.\d+)', gps_info)
        if gps_info:
            lat, lng = map(float, gps_info.groups())
        
        return lat, lng
    
    def read_loop(self):
        
        while True:
            
            while self.TRAILER_CHAR not in self.packet_buffer:
                
                # Read message from the TCP socket
                
                socket_read = self.socket.recv(1024 * 1024 * 10)
                
                if not socket_read and platform in ('cygwin', 'win32'):
                    
                    # Windows user hit Ctrl+C from the terminal, which
                    # subsequently propagated to adb_bridge and killed it.
                    # Try to restart the subprocess in order to perform
                    # the deinitialization sequence well.
                    
                    self._relaunch_adb_bridge()
                    
                    # If restarting adb succeeded, this confirms the idea
                    # than the user did Ctrl+C, so we propagate the actual
                    # Ctrl+C to the main thread.
                    
                    if not self.program_is_terminating:
                        
                        with self.shutdown_event:
                        
                            self.shutdown_event.notify()
                    
                    socket_read = self.socket.recv(1024 * 1024 * 10)
                
                if not socket_read:
                    
                    print('\nThe connection to the adb bridge was closed, or ' +
                        'preempted by another QCSuper instance')
                    
                    return
                
                self.packet_buffer += socket_read
            
            while self.TRAILER_CHAR in self.packet_buffer:
                
                # Parse frame
                
                raw_payload, self.packet_buffer = self.packet_buffer.split(self.TRAILER_CHAR, 1)
                
                # Decapsulate and dispatch
                
                try:
                
                    unframed_message = self.hdlc_decapsulate(
                        payload = raw_payload + self.TRAILER_CHAR,
                        
                        raise_on_invalid_frame = not self.received_first_packet
                    )
                
                except self.InvalidFrameError:
                    
                    # The first packet that we receive over the Diag input may
                    # be partial
                    
                    continue
                
                finally:
                    
                    self.received_first_packet = True
                
                self.dispatch_received_diag_packet(unframed_message)

    def dispose(self, disposing=True):

        if not self._disposed:
            if hasattr(self, 'adb_proc'):
                self.adb_proc.terminate()

            self._disposed = True
