"""
 * Copyright (C) Caleb Marshall - All Rights Reserved
 * Written by Caleb Marshall <anythingtechpro@gmail.com>, August 17th, 2017
 * Licensing information can found in 'LICENSE', which is part of this source code package.
"""
import inspect
import os
import collections
import threading

from direct.showbase.VFSImporter import vfs
from panda3d.core import *
from panda3d.direct import *

from direct.distributed.PyDatagramIterator import PyDatagramIterator

from otp_server.realtime import types
from otp_server.realtime.notifier import notify


class NetworkError(RuntimeError):
    """
    A network specific runtime error
    """


class NetworkDatagram(NetDatagram):
    """
    A class that inherits from panda's C++ NetDatagram buffer.
    This class adds useful methods and functions for talking
    to the OTP's internal cluster participants...
    """

    def add_header(self, channel, sender, message_type):
        self.add_uint8(1)
        self.add_uint64(channel)
        self.add_uint64(sender)
        self.add_uint16(message_type)

    def add_control_header(self, channel, message_type):
        self.add_uint8(1)
        self.add_uint64(types.CONTROL_MESSAGE)
        self.add_uint16(message_type)
        self.add_uint64(channel)


class NetworkDatagramIterator(PyDatagramIterator):
    """
    A class that inherits from panda's C++ DatagramIterator buffer.
    This class adds useful methods and functions for talking
    to the OTP's internal cluster participants...
    """


class NetworkDCLoader(object):
    notify = notify.new_category('NetworkDCLoader')

    def __init__(self):
        self._dc_file = DCFile()
        self._dc_suffix = ''

        self._dclasses_by_name = {}
        self._dclasses_by_number = {}

        self._hash_value = 0

    @property
    def dc_file(self):
        return self._dc_file

    @property
    def dc_suffix(self):
        return self._dc_suffix

    @property
    def dclasses_by_name(self):
        return self._dclasses_by_name

    @property
    def dclasses_by_number(self):
        return self._dclasses_by_number

    @property
    def hash_value(self):
        return self._hash_value

    def read_dc_files(self, dc_file_names=None):
        dc_imports = {}
        if dc_file_names == None:
            read_result = self._dc_file.read_all()
            if not read_result:
                self.notify.error('Could not read dc file.')
        else:
            for dc_fileName in dc_file_names:
                searchPath = DSearchPath()

                # In other environments, including the dev environment, look here:
                searchPath.appendDirectory(Filename('phase_3/etc'))
                base = os.path.expandvars('$TOONTOWN') or './toontown'
                searchPath.appendDirectory(Filename.fromOsSpecific(os.path.expandvars(base + '/src/configfiles')))
                base = os.path.expandvars('$OTP') or './otp'
                searchPath.appendDirectory(Filename.fromOsSpecific(os.path.expandvars(base + '/src/configfiles')))
                # RobotToonManager needs to look for file in current directory
                searchPath.appendDirectory(Filename('.'))

                pathname = Filename(dc_fileName)
                vfs.resolveFilename(pathname, searchPath)

                read_result = self._dc_file.read(pathname)
                if not read_result:
                    self.notify.error('Could not read dc file: %s' % pathname)

        self._hash_value = self._dc_file.get_hash()

        # Now get the class definition for the classes named in the DC
        # file.
        for i in range(self._dc_file.get_num_classes()):
            dclass = self._dc_file.get_class(i)
            number = dclass.get_number()
            class_name = dclass.get_name() + self._dc_suffix

            # Does the class have a definition defined in the newly
            # imported namespace?
            class_def = dc_imports.get(class_name)

            # Also try it without the dc_suffix.
            if class_def is None:
                class_name = dclass.get_name()
                class_def = dc_imports.get(class_name)

            if class_def is None:
                self.notify.debug('No class definition for %s.' % class_name)
            else:
                if inspect.ismodule(class_def):
                    if not hasattr(class_def, class_name):
                        self.notify.error('Module %s does not define class %s.' % (
                            class_name, class_name))

                    class_def = getattr(class_def, class_name)

                if not inspect.isclass(class_def):
                    self.notify.error('Symbol %s is not a class name.' % class_name)
                else:
                    dclass.set_class_def(class_def)

            self._dclasses_by_name[class_name] = dclass
            if number >= 0:
                self._dclasses_by_number[number] = dclass


class NetworkManager(object):
    notify = notify.new_category('NetworkManager')

    def get_unique_name(self, name):
        return '%s-%s-%s' % (self.__class__.__name__, name, id(self))

    def get_puppet_connection_channel(self, doId):
        return doId + (1001 << 32)

    def get_account_connection_channel(self, doId):
        return doId + (1003 << 32)

    def get_account_id_from_channel_code(self, channel):
        return channel >> 32

    def get_avatar_id_from_connection_channel(self, channel):
        return channel & 0xffffffff


class NetworkConnector(NetworkManager):
    notify = notify.new_category('NetworkConnector')

    def __init__(self, dc_loader, address, port, channel, timeout=5000):
        NetworkManager.__init__(self)

        self._dc_loader = dc_loader
        self.__address = address
        self.__port = port
        self._channel = channel
        self.__timeout = timeout

        num_threads = 0
        if config.GetBool('net-want-threads', False):
            num_threads = 1

        self.__manager = QueuedConnectionManager()
        self.__reader = QueuedConnectionReader(self.__manager, num_threads)
        self.__writer = ConnectionWriter(self.__manager, num_threads)

        self.__socket = None
        self._readable = collections.deque()
        self._read_mutex = threading.RLock()

        self.__read_task = None
        self.__update_task = None
        self.__disconnect_task = None

    @property
    def dc_loader(self):
        return self._dc_loader

    @property
    def channel(self):
        return self._channel

    @channel.setter
    def channel(self, channel):
        self._channel = channel

    def setup(self):
        self.__socket = self.__manager.open_TCP_client_connection(self.__address,
                                                                  self.__port, self.__timeout)

        if not self.__socket:
            raise NetworkError('Failed to connect TCP socket on address: <%s:%d>!' % (
                self.__address, self.__port))

        self.__reader.add_connection(self.__socket)
        self.register_for_channel(self._channel)

        self.__read_task = task_mgr.add(self.__read_incoming,
                                        self.get_unique_name('read-incoming'))

        self.__update_task = task_mgr.add(self.__update,
                                          self.get_unique_name('update-handler'))

        self.__disconnect_task = task_mgr.add(self.__listen_disconnect,
                                              self.get_unique_name('listen-disconnect'))

    def register_for_channel(self, channel):
        """
        Registers our connections channel with the MessageDirector
        """

        datagram = NetworkDatagram()
        datagram.add_control_header(channel, types.CONTROL_SET_CHANNEL)
        self.handle_send_connection_datagram(datagram)

    def unregister_for_channel(self, channel):
        """
        Unregisters our connections channel from the MessageDirector
        """

        datagram = NetworkDatagram()
        datagram.add_control_header(channel, types.CONTROL_REMOVE_CHANNEL)
        self.handle_send_connection_datagram(datagram)

    def __read_incoming(self, task):
        """
        Polls for incoming data
        """

        if self.__reader.data_available():
            datagram = NetworkDatagram()

            if self.__reader.get_data(datagram):
                self.__handle_incoming_data(datagram)

        return task.cont

    def __update(self, task):
        """
        Gets a datagram from the queue and handles it
        """

        if not len(self._readable):
            return task.cont

        datagram = self._readable.popleft()
        di = NetworkDatagramIterator(datagram)
        if not di.get_remaining_size():
            return task.cont

        with self._read_mutex:
            self.handle_internal_datagram(di)

        return task.cont

    def __listen_disconnect(self, task):
        """
        Watches our connected socket object and determines if the stream has ended..
        """

        if not self.__reader.is_connection_ok(self.__socket):
            self.handle_disconnected()
            return task.done

        return task.cont

    def __handle_incoming_data(self, datagram):
        """
        Handles incoming data from the connector
        """

        self._readable.append(datagram)

    def handle_send_connection_datagram(self, datagram):
        """
        Sends a datagram to our connection
        """
        self.__writer.send(datagram, self.__socket)

    def handle_internal_datagram(self, di):
        """
        Handles a datagram that was sent by the message director
        """

        code = di.get_uint8()
        self.handle_datagram(di.get_uint64(), di.get_uint64(), di.get_uint16(), di)

    def handle_datagram(self, channel, sender, message_type, di):
        """
        Handles a datagram that was pulled from the queue
        """

    def handle_disconnect(self):
        """
        Disconnects our client socket instance
        """

        self.__manager.close_connection(self.__socket)

    def handle_disconnected(self):
        """
        Handles disconnection when the socket connection closes
        """

        self.unregister_for_channel(self._channel)
        self.__reader.remove_connection(self.__socket)

    def shutdown(self):
        if self.__read_task:
            task_mgr.remove(self.__read_task)

        if self.__update_task:
            task_mgr.remove(self.__update_task)

        if self.__disconnect_task:
            task_mgr.remove(self.__disconnect_task)

        self.__read_task = None
        self.__update_task = None
        self.__disconnect_task = None


class NetworkHandler(NetworkManager):
    notify = notify.new_category('NetworkHandler')

    def __init__(self, network, rendezvous, address, connection, channel=0):
        self._network = network
        self._rendezvous = rendezvous
        self._address = address
        self._connection = connection
        self._channel = channel
        self._allocated_channel = channel

        self._readable = collections.deque()
        self._read_mutex = threading.RLock()

        self.__update_task = None

    @property
    def network(self):
        return self._network

    @property
    def rendezvous(self):
        return self._rendezvous

    @property
    def address(self):
        return self._address

    @property
    def connection(self):
        return self._connection

    @property
    def channel(self):
        return self._channel

    @channel.setter
    def channel(self, channel):
        if not self._channel:
            self._allocated_channel = channel

        self._channel = channel

    @property
    def allocated_channel(self):
        return self._allocated_channel

    @allocated_channel.setter
    def allocated_channel(self, allocated_channel):
        self._allocated_channel = allocated_channel

    def setup(self):
        self.__update_task = task_mgr.add(self.__update,
                                          self.get_unique_name('update-handler'))

        if self._channel:
            self.register_for_channel(self._channel)

    def register_for_channel(self, channel):
        """
        Registers our connections channel with the MessageDirector
        """

        datagram = NetworkDatagram()
        datagram.add_control_header(channel, types.CONTROL_SET_CHANNEL)
        self._network.handle_send_connection_datagram(datagram)
        self._network.add_channel_to_handler(channel, self)

    def unregister_for_channel(self, channel):
        """
        Unregisters our connections channel from the MessageDirector
        """

        datagram = NetworkDatagram()
        datagram.add_control_header(channel, types.CONTROL_REMOVE_CHANNEL)
        self._network.handle_send_connection_datagram(datagram)
        self._network.remove_channel_to_handler(channel)

    def handle_set_channel_id(self, channel):
        # only allow them to unregister another channel they set before,
        # do not allow the allocated channel to be unregistered because we
        # use that channel to determine when the client disconnects...
        if channel != self._channel and self._channel != self._allocated_channel:
            self.unregister_for_channel(self._channel)

        self._channel = channel
        self.register_for_channel(channel)

    def __update(self, task):
        """
        Gets a datagram from the queue and handles it
        """

        if not len(self._readable):
            return task.cont

        datagram = self._readable.popleft()
        di = NetworkDatagramIterator(datagram)
        if not di.get_remaining_size():
            return task.cont

        with self._read_mutex:
            self.handle_datagram(di)

        return task.cont

    def handle_send_datagram(self, datagram):
        """
        Sends a datagram to our connection
        """

        self._network.handle_send_datagram(datagram, self._connection)

    def handle_incoming_data(self, datagram):
        """
        Puts an incoming datagram in the data queue
        """

        self._readable.append(datagram)

    def handle_datagram(self, di):
        """
        Handles a datagram that was pulled from the queue
        """

    def handle_disconnect(self):
        """
        Disconnects our client socket instance
        """
        self._network.handle_disconnect(self)

    def handle_disconnected(self):
        """
        Handles disconnection when the socket connection closes
        """
        self.unregister_for_channel(
            self.get_puppet_connection_channel(self.get_avatar_id_from_connection_channel(self._channel)))
        self.unregister_for_channel(self._channel)
        self._network.handle_disconnected(self)

    def shutdown(self):
        if self._allocated_channel and self._channel != self._allocated_channel:
            self.unregister_for_channel(self._allocated_channel)

        if self._channel:
            self.unregister_for_channel(self._channel)

        if self.__update_task:
            task_mgr.remove(self.__update_task)

        self.__update_task = None


class NetworkListener(NetworkManager):
    notify = notify.new_category('NetworkListener')

    def __init__(self, address, port, handler, backlog=10000):
        NetworkManager.__init__(self)

        self.__address = address
        self.__port = port
        self.__handler = handler
        self.__backlog = backlog

        num_threads = 0
        if config.GetBool('net-want-threads', False):
            num_threads = 1

        self.__manager = QueuedConnectionManager()
        self.__listener = QueuedConnectionListener(self.__manager, num_threads)
        self.__reader = QueuedConnectionReader(self.__manager, num_threads)
        self.__writer = ConnectionWriter(self.__manager, num_threads)

        self.__socket = None
        self._handlers = {}
        self._channel2handlers = {}

        self.__listen_task = None
        self.__read_task = None
        self.__disconnect_task = None

    def setup(self):
        self.__socket = self.__manager.open_TCP_server_rendezvous(self.__address,
                                                                  self.__port, self.__backlog)

        if not self.__socket:
            raise NetworkError('Failed to bind TCP socket on address: <%s:%d>!' % (
                self.__address, self.__port))

        self.__listener.add_connection(self.__socket)

        self.__listen_task = task_mgr.add(self.__listen_incoming,
                                          self.get_unique_name('listen-incoming'))

        self.__read_task = task_mgr.add(self.__read_incoming,
                                        self.get_unique_name('read-incoming'))

        self.__disconnect_task = task_mgr.add(self.__listen_disconnect,
                                              self.get_unique_name('listen-disconnect'))

    def __listen_incoming(self, task):
        """
        Polls for incoming connections
        """

        if self.__listener.new_connection_available():
            rendezvous = PointerToConnection()
            address = NetAddress()
            connection = PointerToConnection()

            if self.__listener.get_new_connection(rendezvous, address, connection):
                self.handle_incoming_connection(rendezvous, address, connection.p())

        return task.cont

    def __read_incoming(self, task):
        """
        Polls for incoming data
        """

        if self.__reader.data_available():
            datagram = NetworkDatagram()

            if self.__reader.get_data(datagram):
                self.__handle_incoming_data(datagram, datagram.get_connection())

        return task.cont

    def __listen_disconnect(self, task):
        """
        Watches all connected socket objects and determines if the stream has ended...
        """

        # ?
        try:
            for handler in self._handlers.values():
                if not self.__reader.is_connection_ok(handler.connection):
                    handler.handle_disconnected()
        except:
            pass

        return task.cont

    def has_handler(self, connection):
        """
        Returns True if the handler is queued else False
        """

        return connection in self._handlers

    def add_handler(self, handler):
        """
        Adds a handler to the handlers dictionary
        """

        if self.has_handler(handler.connection):
            return

        self.__reader.add_connection(handler.connection)
        self._handlers[handler.connection] = handler
        handler.setup()

    def remove_handler(self, handler):
        """
        Removes a handler from the handlers dictionary
        """

        if not self.has_handler(handler.connection):
            return

        handler.shutdown()
        self.__reader.remove_connection(handler.connection)
        del self._handlers[handler.connection]

    def handle_incoming_connection(self, rendezvous, address, connection):
        """
        Handles an incoming connection from the connection listener
        """

        handler = self.__handler(self, rendezvous, address, connection)
        self.add_handler(handler)

    def __handle_incoming_data(self, datagram, connection):
        """
        Handles new data incoming from the connection reader
        """

        if not self.has_handler(connection):
            return

        self._handlers[connection].handle_incoming_data(datagram)

    def has_channel_to_handler(self, channel):
        """
        Returns True if a handler instance if one is associated with that channel else False
        """

        return channel in self._channel2handlers

    def add_channel_to_handler(self, channel, handler):
        """
        Associates a handler with a channel
        """

        if self.has_channel_to_handler(channel):
            return

        self._channel2handlers[channel] = handler

    def remove_channel_to_handler(self, channel):
        """
        Removes association of a channel to a handler
        """

        if not self.has_channel_to_handler(channel):
            return

        del self._channel2handlers[channel]

    def get_handler_from_channel(self, channel):
        """
        Returns a handler instance if one is associated with that channel
        """

        return self._channel2handlers.get(channel)

    def handle_send_datagram(self, datagram, connection):
        """
        Sends a datagram to a specific connection
        """

        if not self.has_handler(connection):
            return

        self.__writer.send(datagram, connection)

    def handle_disconnect(self, handler):
        """
        Disconnects the handlers client socket instance
        """

        self.__manager.close_connection(handler.connection)

    def handle_disconnected(self, handler):
        """
        Handles disconnection of a client socket instance
        """

        self.remove_handler(handler)

    def shutdown(self):
        if self.__listen_task:
            task_mgr.remove(self.__listen_task)

        if self.__read_task:
            task_mgr.remove(self.__read_task)

        if self.__disconnect_task:
            task_mgr.remove(self.__disconnect_task)

        self.__listen_task = None
        self.__read_task = None
        self.__disconnect_task = None

        self.__listener.remove_connection(self.__socket)
