"""
 * Copyright (C) Caleb Marshall - All Rights Reserved
 * Written by Caleb Marshall <anythingtechpro@gmail.com>, August 17th, 2017
 * Contributed to by Prince Frizzy <theclashingfritz@gmail.com>, June 21st, 2018
 * Licensing information can found in 'LICENSE', which is part of this source code package.
"""

import collections
import itertools
import random

from panda3d.direct import *

from otp_server.realtime import io
from otp_server.realtime import types
from otp_server.realtime.notifier import notify
from otp_server.realtime import util

from otp_server.game.OtpDoGlobals import *
from otp_server.game import ZoneUtil


class SimpleContextQueue:
    def __init__(self):
        self.ack_contexts = []


class StateObject(object):
    notify = notify.new_category('StateObject')

    def __init__(self, network, object_manager, do_id, parent_id, zone_id, dc_class, has_other=False, di=None):
        self._network = network
        self.object_manager = object_manager

        self._do_id = do_id

        self._old_ai_channel = 0
        self._ai_channel = 0

        self._old_owner_id = 0
        self._owner_id = 0

        self._old_parent_id = 0
        self._parent_id = parent_id

        self._old_zone_id = 0
        self._zone_id = zone_id

        self._dc_class = dc_class
        self._has_other = has_other

        self._required_fields = {}
        self._other_fields = {}

        self._zone_objects = {}
        self._watch_list = {}

        if di is not None:
            field_packer = DCPacker()
            field_packer.set_unpack_data(di.get_remaining_bytes())

            for field_index in range(self._dc_class.get_num_inherited_fields()):
                field = self._dc_class.get_inherited_field(field_index)
                if not field:
                    self.notify.error('Failed to unpack required field: %d dclass: %s, unknown field!' % (
                    field_index, self._dc_class.get_name()))

                if not field.is_required():
                    continue

                field_packer.begin_unpack(field)
                field_args = field.unpack_args(field_packer)
                field_packer.end_unpack()

                self._required_fields[field.get_number()] = field_args

            if self._has_other:
                num_fields = field_packer.raw_unpack_uint16()
                for _ in range(num_fields):
                    field_id = field_packer.raw_unpack_uint16()
                    field = self._dc_class.get_field_by_index(field_id)
                    if not field:
                        self.notify.error('Failed to unpack other field: %d dclass: %s, unknown field!' % (
                        field_id, self._dc_class.get_name()))

                    if not field.is_ram():
                        continue

                    field_packer.begin_unpack(field)
                    field_args = field.unpack_args(field_packer)
                    field_packer.end_unpack()

                    self._other_fields[field.get_number()] = field_args

            self._network.register_for_channel(self._do_id)

    @property
    def do_id(self):
        return self._do_id

    @property
    def old_ai_channel(self):
        return self._old_ai_channel

    @property
    def ai_channel(self):
        return self._ai_channel

    @ai_channel.setter
    def ai_channel(self, ai_channel):
        self._old_ai_channel = self._ai_channel
        self._ai_channel = ai_channel

    @property
    def old_owner_id(self):
        return self._old_owner_id

    @property
    def owner_id(self):
        return self._owner_id

    @owner_id.setter
    def owner_id(self, owner_id):
        self._old_owner_id = self._owner_id
        self._owner_id = owner_id

    @property
    def old_parent_id(self):
        return self._old_parent_id

    @property
    def parent_id(self):
        return self._parent_id

    @parent_id.setter
    def parent_id(self, parent_id):
        self._old_parent_id = self._parent_id
        self._parent_id = parent_id

    @property
    def old_zone_id(self):
        return self._old_zone_id

    @property
    def zone_id(self):
        return self._zone_id

    @zone_id.setter
    def zone_id(self, zone_id):
        self._old_zone_id = self._zone_id
        self._zone_id = zone_id

    @property
    def dc_class(self):
        return self._dc_class

    @property
    def has_other(self):
        return self._has_other

    def has_child(self, child_do_id):
        return self.get_zone_from_child(child_do_id) is not None

    def has_child_in_zone(self, child_do_id, zone_id):
        return self.get_zone_from_child(child_do_id) == zone_id

    def add_child_in_zone(self, child_do_id, zone_id):
        zone_objects = self._zone_objects.setdefault(zone_id, [])
        zone_objects.append(child_do_id)

    def remove_child_from_zone(self, child_do_id, zone_id):
        zone_objects = self._zone_objects.get(zone_id, None)
        assert (zone_objects != None)
        if child_do_id in zone_objects:
            zone_objects.remove(child_do_id)

        if not len(zone_objects):
            del self._zone_objects[zone_id]

    def get_zone_from_child(self, child_do_id):
        for zone_id, zone_objects in list(self._zone_objects.items()):
            if child_do_id in zone_objects:
                return zone_id

        return None

    def get_zone_objects(self, zone_id):
        if zone_id not in self._zone_objects:
            return []

        zone_objects = []
        for do_id in list(self._zone_objects[zone_id]):
            zone_object = self._network.object_manager.get_object(do_id)
            if not zone_object:
                continue

            zone_objects.append(zone_object)

        return zone_objects

    def get_all_zone_objects(self):
        zone_objects = []
        for zone_id in list(self._zone_objects):
            zone_objects.extend(self.get_zone_objects(zone_id))

        return zone_objects

    def get_zones_objects(self, zone_ids):
        zone_objects = []
        for zone_id in zone_ids:
            zone_objects.extend(self.get_zone_objects(zone_id))

        return zone_objects

    def append_required_data(self, datagram, broadcast_only=True):
        sorted_fields = collections.OrderedDict(sorted(
            self._required_fields.items()))

        field_packer = DCPacker()
        for field_index, field_args in list(sorted_fields.items()):
            field = self._dc_class.get_field_by_index(field_index)
            if not field:
                self.notify.error('Failed to append required data for field: %d  dclass: %s, unknown field!' % (
                field_index, self._dc_class.get_name()))

            if broadcast_only and not field.is_broadcast():
                continue

            field_packer.begin_pack(field)
            field.pack_args(field_packer, field_args)
            field_packer.end_pack()

        datagram.append_data(field_packer.get_string())

    def append_other_data(self, datagram):
        field_packer = DCPacker()
        for field_index, field_args in list(self._other_fields.items()):
            field = self._dc_class.get_field_by_index(field_index)
            if not field:
                self.notify.error('Failed to append other data for field: %d  dclass: %s, unknown field!' % (
                field_index, self._dc_class.get_name()))

            field_packer.raw_pack_uint16(field.get_number())
            field_packer.begin_pack(field)
            field.pack_args(field_packer, field_args)
            field_packer.end_pack()

        datagram.add_uint16(len(self._other_fields))
        datagram.append_data(field_packer.get_string())

    def setup(self):
        self.object_manager.handle_changing_location(self)

    def handle_internal_datagram(self, sender, message_type, di):
        if message_type == types.STATESERVER_OBJECT_SET_OWNER_RECV:
            self.handle_set_owner(sender, di)
        elif message_type == types.STATESERVER_OBJECT_SET_AI:
            self.handle_set_ai(sender, di)
        elif message_type == types.STATESERVER_OBJECT_SET_ZONE:
            self.handle_set_zone(sender, di)
        elif message_type == types.STATESERVER_OBJECT_CHANGING_LOCATION:
            self.handle_changing_location(di.get_uint32(), di.get_uint32(), di.get_uint32())
        elif message_type == types.STATESERVER_OBJECT_SET_LOCATION:
            self.handle_set_location(sender, di)
        elif message_type == types.STATESERVER_OBJECT_GET_ZONES_OBJECTS:
            self.handle_get_zones_objects(sender, di)
        elif message_type == types.STATESERVER_OBJECT_GET_ZONES_OBJECTS_2:
            self.handle_get_zones_objects_2(sender, di)
        elif message_type == types.STATESERVER_OBJECT_CLEAR_WATCH:
            self.handle_clear_watch(sender, di)
        else:
            self.notify.warning('Received unknown message type: %d for object %d!' % (message_type, self._do_id))
            return

    def handle_send_changing_owner(self, channel, old_owner_id, new_owner_id):
        datagram = io.NetworkDatagram()
        datagram.add_header(channel, self._do_id, types.STATESERVER_OBJECT_CHANGE_OWNER_RECV)

        datagram.add_uint64(self._do_id)
        datagram.add_uint64(new_owner_id)
        datagram.add_uint64(old_owner_id)
        self._network.handle_send_connection_datagram(datagram)

    def handle_send_owner_entry(self, channel):
        datagram = io.NetworkDatagram()

        datagram.add_header(channel, self._do_id, types.STATESERVER_OBJECT_ENTER_OWNER_RECV)
        datagram.add_uint64(self._do_id)
        datagram.add_uint64(self._parent_id)
        datagram.add_uint32(self._zone_id)
        datagram.add_uint16(self._dc_class.get_number())

        self.append_required_data(datagram, broadcast_only=False)
        if self._has_other:
            self.append_other_data(datagram)

        self._network.handle_send_connection_datagram(datagram)

    def handle_set_owner(self, sender, di):
        new_owner_id = di.get_uint64()
        if new_owner_id == self._owner_id:
            return

        self.owner_id = new_owner_id
        self.handle_send_owner_entry(self._owner_id)
        self.handle_send_changing_owner(self._old_owner_id, self._old_owner_id, self._owner_id)

    def handle_send_changing_ai(self, channel):
        datagram = io.NetworkDatagram()
        datagram.add_header(channel, self._do_id, types.STATESERVER_OBJECT_CHANGING_AI)

        datagram.add_uint64(self._do_id)
        datagram.add_uint64(self._old_ai_channel)
        datagram.add_uint64(self._ai_channel)
        self._network.handle_send_connection_datagram(datagram)

    def handle_send_ai_entry(self, ai_channel):
        datagram = io.NetworkDatagram()
        if not self._has_other:
            datagram.add_header(ai_channel, self._do_id, types.STATESERVER_OBJECT_ENTER_AI_WITH_REQUIRED)
        else:
            datagram.add_header(ai_channel, self._do_id, types.STATESERVER_OBJECT_ENTER_AI_WITH_REQUIRED_OTHER)

        datagram.add_uint64(self._do_id)
        datagram.add_uint64(self._parent_id)
        datagram.add_uint32(self._zone_id)
        datagram.add_uint16(self._dc_class.get_number())

        self.append_required_data(datagram, broadcast_only=not self._owner_id)
        if self._has_other:
            self.append_other_data(datagram)

        self._network.handle_send_connection_datagram(datagram)

    def handle_set_ai(self, sender, di):
        new_ai_channel = di.get_uint64()

        shard = self._network.shard_manager.get_shard(new_ai_channel)
        if not shard:
            self.notify.warning('Failed to set new AI: %d for object %d, '
                                'no AI was found with that channel!' % (new_ai_channel, self._do_id))

            return

        if di.get_remaining_bytes():
            context = di.get_uint32()
            self.object_manager.context_queue.ack_contexts.append(context)
            zoneId = di.get_uint32()
            if zoneId > 999:
                self.zone_id = zoneId
                self.handle_send_changing_location(self._ai_channel)

        if new_ai_channel != self._ai_channel:
            self.ai_channel = new_ai_channel
            if self._owner_id:
                self.parent_id = shard.district_id

            self.handle_send_ai_entry(self._ai_channel)
            self.handle_send_changing_ai(self.old_ai_channel)

        self.object_manager.handle_changing_location(self)

    def handle_send_changing_location(self, channel):
        if sender != self.owner_id:
            if self.old_parent_id == self.parent_id:
                datagram = io.NetworkDatagram()
                datagram.add_header(channel, self._do_id, types.STATESERVER_OBJECT_CHANGE_ZONE)
                datagram.add_uint32(self._do_id)
                datagram.add_uint32(self.parent_id)
                datagram.add_uint32(self.zone_id)
                datagram.add_uint32(self.old_parent_id)
                datagram.add_uint32(self.old_zone_id)
                self._network.handle_send_connection_datagram(datagram)
                return

        # Delete the old object.
        self.handle_send_departure(channel)

        # Send a required other enter zone response.
        datagram = io.NetworkDatagram()
        datagram.add_header(channel, self._do_id, types.STATESERVER_OBJECT_ENTERZONE_WITH_REQUIRED_OTHER)
        datagram.add_uint32(self.parent_id)
        datagram.add_uint32(self.zone_id)
        datagram.add_uint16(self._dc_class.get_number())
        datagram.add_uint32(self._do_id)

        self.append_required_data(datagram, broadcast_only=not self._owner_id)
        if self._has_other:
            self.append_other_data(datagram)

        self._network.handle_send_connection_datagram(datagram)

    def handle_set_zone(self, sender, di):
        self.old_parent_id = self.parent_id
        self.old_zone_id = self.zone_id

        new_parent_id = di.get_uint32()
        new_zone_id = di.get_uint32()

        self.parent_id = new_parent_id
        self.zone_id = new_zone_id

        self.handle_send_changing_location(self._ai_channel)
        self.object_manager.handle_changing_location(self)

    def handle_send_location_entry(self, channel):
        datagram = io.NetworkDatagram()
        if not self._has_other:
            datagram.add_header(channel, self._do_id, types.STATESERVER_OBJECT_ENTER_LOCATION_WITH_REQUIRED)
        else:
            datagram.add_header(channel, self._do_id, types.STATESERVER_OBJECT_ENTER_LOCATION_WITH_REQUIRED_OTHER)

        datagram.add_uint64(self._do_id)
        datagram.add_uint64(self._parent_id)
        datagram.add_uint32(self._zone_id)
        datagram.add_uint16(self._dc_class.get_number())

        self.append_required_data(datagram)
        if self._has_other:
            self.append_other_data(datagram)

        self._network.handle_send_connection_datagram(datagram)

    def handle_send_departure(self, channel):
        datagram = io.NetworkDatagram()
        datagram.add_header(channel, self._do_id,
                            types.STATESERVER_OBJECT_DELETE_RAM)

        datagram.add_uint32(self._do_id)
        self._network.handle_send_connection_datagram(datagram)

    def handle_send_object_location_ack(self, channel):
        datagram = io.NetworkDatagram()
        datagram.add_header(channel, self._do_id, types.STATESERVER_OBJECT_LOCATION_ACK)

        datagram.add_uint32(self._do_id)
        datagram.add_uint32(self._old_parent_id)
        datagram.add_uint32(self._old_zone_id)
        datagram.add_uint32(self._parent_id)
        datagram.add_uint32(self._zone_id)
        if len(self.object_manager.context_queue.ack_contexts) > 0:
            context = self.object_manager.context_queue.ack_contexts.pop(0)
            datagram.add_uint32(context)
        else:
            datagram.add_uint32(0)
        self._network.handle_send_connection_datagram(datagram)

    def has_zone_watcher(self, zone_id):
        for sender in self._watch_list.keys():
            if zone_id in self._watch_list[sender]:
                return (True, sender)

        return (False, None)

    def handle_changing_location(self, child_do_id, new_parent_id, new_zone_id):
        # retrieve this object from it's do_id, if we cannot find this object in the do_id to do
        # dictionary, then this is an invalid object...
        child_object = self.object_manager.get_object(child_do_id)
        if not child_object:
            return

        send_location_entry = False
        send_location_departure = False
        if self.has_child(child_object.do_id):
            child_zone_id = self.get_zone_from_child(child_object.do_id)
            if new_parent_id != self._do_id:
                self.remove_child_from_zone(child_object.do_id, child_zone_id)
                send_location_departure = True
            elif new_zone_id != child_zone_id:
                self.remove_child_from_zone(child_object.do_id, child_zone_id)
                self.add_child_in_zone(child_object.do_id, new_zone_id)
                send_location_entry = True
                send_location_departure = True
        else:
            self.add_child_in_zone(child_object.do_id, new_zone_id)
            send_location_entry = True

        # if self.object_manager.tracking == child_object.do_id:
        #    print "yEEEEEEEEEEEEEs ", child_zone_id, new_zone_id

        # if this object is entering the new zone, then relay a location
        # generate to everyone in the new zone.
        if send_location_entry:
            zone_objects = self.get_zone_objects(new_zone_id)

            for zone_object in itertools.ifilter(lambda x: x.owner_id > 0, zone_objects):
                child_object.handle_send_location_entry(zone_object.owner_id)

            has_watcher, watcher = self.has_zone_watcher(new_zone_id)
            if has_watcher:
                child_object.handle_send_location_entry(watcher)

        # also send a departure to everyone in the object's old zone...
        if send_location_departure:
            zone_objects = self.get_zone_objects(child_zone_id)

            has_watcher_at_new, watcher = self.has_zone_watcher(new_zone_id)

            for zone_object in itertools.ifilter(lambda x: x.owner_id > 0, zone_objects):
                if child_object.do_id != self._do_id:
                    if (has_watcher_at_new and zone_object.owner_id != watcher) or not has_watcher_at_new:
                        child_object.handle_send_departure(zone_object.owner_id)

            if has_watcher_at_new:
                child_object.handle_send_changing_location(watcher)

        # acknowledge the object's location change was successful.
        if child_object.owner_id:
            child_object.handle_send_object_location_ack(child_object.owner_id)

    def handle_set_location(self, sender, di):
        new_parent_id = di.get_uint32()
        new_zone_id = di.get_uint32()
        if new_parent_id == self._parent_id and new_zone_id == self._zone_id:
            return

        self.parent_id = new_parent_id
        self.zone_id = new_zone_id

        self.handle_changing_location(self._zone_id, new_parent_id, new_zone_id)
        if self.parent_id:
            self.handle_send_changing_location(self.parent_id)
        # self.object_manager.handle_changing_location(self)

    def handle_get_zones_objects(self, sender, di):
        zone_ids = [di.get_uint32() for _ in range(di.get_uint16())]
        if not self._owner_id:
            self.notify.warning('Cannot get zone objects for object: %d, object does not have an owner!' % self._do_id)
            return

        parent_object = self._network.object_manager.get_object(self._parent_id)
        if not parent_object:
            self.notify.warning('Cannot get zone objects for object: %d, object has no parent!' % self._do_id)
            return

        # filter out our own object from the zone list, as we do not want
        # to send our own object because we have reference to it locally...
        zone_objects = list(itertools.ifilter(lambda x: x.do_id != self._do_id,
                                              parent_object.get_zones_objects(zone_ids)))

        # tell the Client Agent that the they should expect this
        # many objects to have been generated before completing the zone change...
        datagram = io.NetworkDatagram()
        datagram.add_header(self._owner_id, self._do_id,
                            types.STATESERVER_OBJECT_GET_ZONES_OBJECTS_RESP)

        datagram.add_uint64(self._do_id)
        datagram.add_uint16(len(zone_objects))
        for zone_object in zone_objects:
            datagram.add_uint64(zone_object.do_id)

        self._network.handle_send_connection_datagram(datagram)

        # finally once we've sent the objects we expect the client,
        # to see before completing the interest change, start sending object generates...
        for zone_object in zone_objects:
            zone_object.handle_send_location_entry(self._owner_id)

    def handle_get_zones_objects_2(self, sender, di):
        contextId = di.get_uint32()
        zone_ids = [di.get_uint32() for _ in range(di.get_uint16())]

        zone_objects = self.get_zones_objects(zone_ids)
        parent_object = self._network.object_manager.get_object(self._parent_id)
        if parent_object:
            pobjs = parent_object.get_zones_objects(zone_ids)
            for p in pobjs:
                if p.do_id != self._do_id and p.do_id != parent_object.do_id:
                    zone_objects.append(p)

        s = sender
        if self._owner_id:
            s = self._owner_id

        # tell the Client Agent that the they should expect this
        # many objects to have been generated before completing the zone change...
        datagram = io.NetworkDatagram()
        datagram.add_header(s, self._do_id,
                            types.STATESERVER_OBJECT_GET_ZONES_OBJECTS_2_RESP)

        datagram.add_uint32(contextId)
        datagram.add_uint16(len(zone_objects))
        for zone_object in zone_objects:
            if zone_object.dc_class.get_name() == "DistributedSuit" and not self.object_manager.tracking:
                print("will be keeping track of suit %d at %d" % (zone_object.do_id, zone_object.zone_id))
                self.object_manager.tracking = zone_object.do_id
            datagram.add_uint64(zone_object.do_id)

        self._network.handle_send_connection_datagram(datagram)

        # once we've sent the objects we expect the client,
        # to see before completing the interest change, start sending object generates...
        for zone_object in zone_objects:
            zone_object.handle_send_location_entry(sender)

        # now add the zone ids to the sender's watch list
        if not self._watch_list.has_key(sender):
            self._watch_list[sender] = []
        for zone_id in zone_ids:
            if zone_id not in self._watch_list[sender]:
                self._watch_list[sender].append(zone_id)

    def handle_clear_watch(self, sender, di):
        if self._watch_list.has_key(sender):
            zone_id = di.get_uint32()
            if zone_id in self._watch_list[sender]:
                self._watch_list[sender].remove(zone_id)
        else:
            self.notify.warning("Sender %d tried to clear watch zone but has no watch list!" % sender)

    def handle_send_update_field(self, channel, sender, field, field_args):
        datagram = io.NetworkDatagram()
        datagram.add_header(channel, sender,
                            types.STATESERVER_OBJECT_UPDATE_FIELD)

        datagram.add_uint32(self._do_id)
        datagram.add_uint16(field.get_number())

        field_packer = DCPacker()
        field_packer.begin_pack(field)
        if field_args is not None:
            field.pack_args(field_packer, field_args)

        field_packer.end_pack()

        datagram.append_data(field_packer.get_string())
        self._network.handle_send_connection_datagram(datagram)

    def handle_send_save_field(self, field, field_args):
        datagram = io.NetworkDatagram()
        datagram.add_header(types.DATABASE_CHANNEL, self._do_id,
                            types.DBSERVER_OBJECT_SET_FIELD)

        datagram.add_uint32(self._do_id)

        field_packer = DCPacker()
        field_packer.raw_pack_uint16(field.get_number())
        field_packer.begin_pack(field)
        field.pack_args(field_packer, field_args)
        field_packer.end_pack()

        datagram.append_data(field_packer.get_string())
        self._network.handle_send_connection_datagram(datagram)

    def handle_update_field(self, channel, sender, di):
        field_id = di.get_uint16()
        field = self._dc_class.get_field_by_index(field_id)
        if not field:
            self.notify.warning('Failed to update field: %d dclass: %s, '
                                'unknown field!' % (field_id, self._dc_class.get_name()))

            return

        datagram = io.NetworkDatagram()
        datagram.append_data(di.get_remaining_bytes())
        di = io.NetworkDatagramIterator(datagram)

        # if the iterator is empty, this means that the field
        # has no arguents and that we should not attempt to update it...
        if di.get_remaining_size():
            field_packer = DCPacker()
            field_packer.set_unpack_data(di.get_remaining_bytes())

            try:
                field_packer.begin_unpack(field)
                field_args = field.unpack_args(field_packer)
                field_packer.end_unpack()
            except RuntimeError:
                # apparently we failed to unpack the arguments for
                # this field we recieved, ignore the update...
                return
        else:
            field_args = None

        di = io.NetworkDatagramIterator(datagram)
        # if field.is_bogus_field():
        #    self.notify.warning('Cannot handle field update for field: %s dclass: %s, field is bogus!' % (
        #        field.get_name(), self._dc_class.get_name()))
        #
        #    return

        if not self._network.shard_manager.has_shard(sender) and sender != types.UD_CHANNEL:
            avatar_id = self._network.get_avatar_id_from_connection_channel(sender)
            if not avatar_id:
                self.notify.warning('Cannot handle field update for field: %s dclass: %s, '
                                    'unknown avatar: %d!' % (field.get_name(), self._dc_class.get_name(), avatar_id))

                return

            # check to ensure the client can send this field, if the field
            # is marked ownsend; the field can only be sent by the owner of the object,
            # if the field is marked clsend the field is sendable always. Otherwise
            # if the client sends the field and it is not marked either of these,
            # the field update is invalid and the field is not sendable by a client...
            if field.is_ownsend():
                if sender != self._owner_id:
                    self.notify.warning('Cannot handle field update for field: %s '
                                        'dclass: %s, field not sendable!' % (
                                        field.get_name(), self._dc_class.get_name()))

                    return
            else:
                if not field.is_clsend():
                    self.notify.warning('Cannot handle field update for field: %s '
                                        'dclass: %s, field not sendable!' % (
                                        field.get_name(), self._dc_class.get_name()))

                    return

            # we must always send this update to the other receiver,
            # so that they get the field update always even if the field
            # is broadcasted to other objects in the same interest...
            self.handle_send_update_field(self._ai_channel, sender, field, field_args)

            # if the field is marked broadcast, then we can proceed to broadcast
            # this field to any other objects in our interest.
            if field.is_broadcast():
                self.object_manager.handle_updating_field(self, sender, field, field_args, excludes=[avatar_id])

            if field_args is not None:
                # the client has sent an broadcast field that is marked ram,
                # store this field since it passes both the is clsend or is ownsend tests...
                if field.is_ram():
                    # ensure the object the client sent the field update for
                    # has other fields...
                    if not self._has_other:
                        return

                    # check to see if this field is a required field, if it is then
                    # this means it should be stored as a required field....
                    if field.is_required():
                        self._required_fields[field.get_number()] = field_args
                    else:
                        self._other_fields[field.get_number()] = field_args
        else:
            # we must always send this update to the other receiver,
            # so that they get the field update always even if the field
            # is broadcasted to other objects in the same interest...
            self.handle_send_update_field(self._owner_id, self._ai_channel, field, field_args)

            # if the field is marked broadcast, then we can proceed to broadcast
            # this field to any other objects in our interest.
            if field.is_broadcast():
                self.object_manager.handle_updating_field(self, self._parent_id, field, field_args,
                                                          excludes=[self.do_id])

            if field_args is not None:
                # if the AI object sends specifically other (ram) fields for this object,
                # this means the object now has other fields...
                if field.is_ram():
                    # check to see if this field is a required field, if it is then
                    # this means it should be stored as a required field....
                    if field.is_required():
                        self._required_fields[field.get_number()] = field_args
                    else:
                        self._other_fields[field.get_number()] = field_args

                    # the object now has other fields, let's update the object's has_other
                    # value so that generates will be sent including the other fields...
                    self._has_other = True

                # check to see if the field is marked db, this means that we send the field
                # to the database to override any current fields with that value...
                if field.is_db():
                    self.handle_send_save_field(field, field_args)

    def destroy(self):
        self.owner_id = 0
        self.parent_id = 0
        self.zone_id = 0

        # manually clear the object's interest and the object's
        # existance on an AI if it exists on one...
        self.handle_send_departure(self._ai_channel)
        parent_object = self._network.object_manager.get_object(self._old_parent_id)
        if parent_object is not None:
            parent_object.handle_changing_location(self._do_id, self._parent_id, self._zone_id)

        self._required_fields = {}
        self._other_fields = {}

        self._zone_objects = {}


class StateObjectManager(object):
    notify = notify.new_category('StateObjectManager')

    def __init__(self):
        self.objects = {}
        self.context_queue = SimpleContextQueue()
        self.tracking = None

    def has_object(self, do_id):
        return do_id in self.objects

    def add_object(self, state_object):
        if self.has_object(state_object.do_id):
            return

        self.objects[state_object.do_id] = state_object
        state_object.setup()

    def remove_object(self, state_object):
        if not self.has_object(state_object.do_id):
            return

        state_object.destroy()
        del self.objects[state_object.do_id]

    def get_object(self, do_id):
        return self.objects.get(do_id)

    def handle_changing_location(self, state_object):
        assert (state_object != None)
        # tell the object's previous parent that we've moved away from under
        # them and are no longer in the previous location...
        if state_object.old_parent_id:
            state_object.handle_send_changing_location(state_object.old_parent_id)

        # also tell the object's new parent that they have changed locations under
        # them in a zone of their's...
        if state_object.parent_id:
            state_object.handle_send_changing_location(state_object.parent_id)

    def handle_updating_field(self, state_object, sender, field, field_args, excludes=[]):
        assert (state_object != None)
        if not state_object.parent_id:
            self.notify.debug('Cannot handle updating field for object: %d, object has no parent!' % state_object.do_id)
            return

        parent_object = self.get_object(state_object.parent_id)
        if not parent_object:
            self.notify.debug('Cannot handle updating field for object: %d, object has no parent!' % state_object.do_id)
            return

        if not parent_object.has_child(state_object.do_id):
            return

        child_zone_id = parent_object.get_zone_from_child(state_object.do_id)
        for zone_object in itertools.ifilter(lambda x: x.owner_id > 0 and x.do_id not in excludes,
                                             parent_object.get_all_zone_objects()):
            state_object.handle_send_update_field(zone_object.owner_id, state_object.do_id, field, field_args)


class StateServer(io.NetworkConnector):
    notify = notify.new_category('StateServer')

    def __init__(self, *args, **kwargs):
        io.NetworkConnector.__init__(self, *args, **kwargs)

        self.object_manager = StateObjectManager()

        # Create our Object Server.
        object_server = StateObject(self, self.object_manager, 20100000, 0, 0,
                                    self.dc_loader.dclasses_by_name.get("ObjectServer"))
        self.object_manager.add_object(object_server)

        # Create our Central Logger.
        central_logger = StateObject(self, self.object_manager, 4688, 0, 0,
                                     self.dc_loader.dclasses_by_name.get("CentralLogger"))
        self.object_manager.add_object(central_logger)

    def handle_datagram(self, channel, sender, message_type, di):
        print("Handling datagram from %d, %d, with message type %d!" % (channel, sender, message_type))
        if message_type == types.STATESERVER_OBJECT_GENERATE_WITH_REQUIRED:
            self.handle_generate(sender, False, di)
        elif message_type == types.STATESERVER_OBJECT_GENERATE_WITH_REQUIRED_OTHER:
            self.handle_generate(sender, True, di)
        elif message_type == types.STATESERVER_OBJECT_UPDATE_FIELD:
            self.handle_object_update_field(channel, sender, di)
        elif message_type == types.STATESERVER_OBJECT_DELETE_RAM:
            self.handle_delete_object(sender, di)
        elif message_type == types.STATESERVER_BOUNCE_MESSAGE:
            print("Bouncy boi!")
        else:
            self.handle_object_datagram(channel, sender, message_type, di)

    def handle_object_datagram(self, channel, sender, message_type, di):
        state_object = self.object_manager.get_object(channel)
        if not state_object:
            self.notify.debug('Received an unknown message type: %d from channel: %d!' % (message_type, sender))
            return

        state_object.handle_internal_datagram(sender, message_type, di)

    def handle_send_disconnect(self, channel, shard):
        print("Terminating shard %d, %d!" % (channel, shard))
        datagram = io.NetworkDatagram()
        datagram.add_header(channel, self.channel, types.CLIENT_AGENT_DISCONNECT)

        datagram.add_uint16(types.CLIENT_DISCONNECT_SHARD_CLOSED)
        datagram.add_string('Shard with channel: %d has been terminated!' % shard.channel)
        self.handle_send_connection_datagram(datagram)

    def handle_generate(self, sender, has_other, di):
        do_id = di.get_uint32()
        parent_id = di.get_uint32()
        zone_id = di.get_uint32()
        dc_id = di.get_uint16()

        if self.object_manager.has_object(do_id):
            self.notify.info('Failed to generate an already existing object with do_id: %d!' % do_id)
            return

        dc_class = self.dc_loader.dclasses_by_number.get(dc_id)
        if not dc_class:
            self.notify.warning(
                'Failed to generate an object with do_id: %d, no dclass found for dc_id: %d!' % (do_id, dc_id))
            return

        state_object = StateObject(self, self.object_manager, do_id, parent_id, zone_id, dc_class, has_other, di)

        # TODO FIXME: find a better way to do this...
        if sender == types.UD_CHANNEL:
            state_object.ai_channel = sender

        self.object_manager.add_object(state_object)

    def handle_object_update_field(self, channel, sender, di):
        do_id = di.get_uint32()
        if not di.get_remaining_size():
            self.notify.warning('Cannot handle an field update for object: %d, truncated datagram!' % do_id)
            return

        state_object = self.object_manager.get_object(do_id)
        if not state_object:
            self.notify.debug('Cannot handle an field update for object: %d, unknown object!' % do_id)
            return

        state_object.handle_update_field(channel, sender, di)

    def handle_delete_object(self, sender, di):
        do_id = di.get_uint32()
        state_object = self.object_manager.get_object(do_id)
        if not state_object:
            self.notify.debug('Failed to delete object: %d, object does not exist!' % do_id)
            return

        self.object_manager.remove_object(state_object)
