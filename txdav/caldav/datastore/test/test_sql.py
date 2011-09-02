##
# Copyright (c) 2010-2011 Apple Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
##

"""
Tests for txdav.caldav.datastore.postgres, mostly based on
L{txdav.caldav.datastore.test.common}.
"""

from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.internet.task import deferLater
from twisted.python import hashlib
from twisted.trial import unittest

from twext.enterprise.dal.syntax import Select, Parameter
from twext.python.vcomponent import VComponent
from twext.web2.dav.element.rfc2518 import GETContentLanguage, ResourceType

from txdav.base.propertystore.base import PropertyName
from txdav.caldav.datastore.test.common import CommonTests as CalendarCommonTests,\
    event4_text
from txdav.caldav.datastore.test.test_file import setUpCalendarStore
from txdav.caldav.datastore.util import _migrateCalendar, migrateHome
from txdav.common.datastore.sql import ECALENDARTYPE
from txdav.common.datastore.sql_tables import schema
from txdav.common.datastore.test.util import buildStore, populateCalendarsFrom

from twistedcaldav import caldavxml

from twistedcaldav.dateops import datetimeMktime
from twistedcaldav.sharing import SharedCollectionRecord

import datetime

class CalendarSQLStorageTests(CalendarCommonTests, unittest.TestCase):
    """
    Calendar SQL storage tests.
    """

    @inlineCallbacks
    def setUp(self):
        yield super(CalendarSQLStorageTests, self).setUp()
        self._sqlCalendarStore = yield buildStore(self, self.notifierFactory)
        yield self.populate()


    @inlineCallbacks
    def populate(self):
        yield populateCalendarsFrom(self.requirements, self.storeUnderTest())
        self.notifierFactory.reset()


    def storeUnderTest(self):
        """
        Create and return a L{CalendarStore} for testing.
        """
        return self._sqlCalendarStore


    @inlineCallbacks
    def assertCalendarsSimilar(self, a, b, bCalendarFilter=None):
        """
        Assert that two calendars have a similar structure (contain the same
        events).
        """
        @inlineCallbacks
        def namesAndComponents(x, filter=lambda x: x.component()):
            result = {}
            for fromObj in (yield x.calendarObjects()):
                result[fromObj.name()] = yield filter(fromObj)
            returnValue(result)
        if bCalendarFilter is not None:
            extra = [bCalendarFilter]
        else:
            extra = []
        self.assertEquals((yield namesAndComponents(a)),
                          (yield namesAndComponents(b, *extra)))


    def assertPropertiesSimilar(self, a, b, disregard=[]):
        """
        Assert that two objects with C{properties} methods have similar
        properties.

        @param disregard: a list of L{PropertyName} keys to discard from both
            input and output.
        """
        def sanitize(x):
            result = dict(x.properties().items())
            for key in disregard:
                result.pop(key, None)
            return result
        self.assertEquals(sanitize(a), sanitize(b))


    def fileTransaction(self):
        """
        Create a file-backed calendar transaction, for migration testing.
        """
        setUpCalendarStore(self)
        fileStore = self.calendarStore
        txn = fileStore.newTransaction()
        self.addCleanup(txn.commit)
        return txn


    @inlineCallbacks
    def test_attachmentPath(self):
        """
        L{ICalendarObject.createAttachmentWithName} will store an
        L{IAttachment} object that can be retrieved by
        L{ICalendarObject.attachmentWithName}.
        """
        yield self.createAttachmentTest(lambda x: x)
        attachmentRoot = (
            yield self.calendarObjectUnderTest()
        )._txn._store.attachmentsPath
        obj = yield self.calendarObjectUnderTest()
        hasheduid = hashlib.md5(obj._dropboxID).hexdigest()
        attachmentPath = attachmentRoot.child(
            hasheduid[0:2]).child(hasheduid[2:4]).child(hasheduid).child(
                "new.attachment")
        self.assertTrue(attachmentPath.isfile())


    @inlineCallbacks
    def test_migrateCalendarFromFile(self):
        """
        C{_migrateCalendar()} can migrate a file-backed calendar to a database-
        backed calendar.
        """
        fromCalendar = yield (yield self.fileTransaction().calendarHomeWithUID(
            "home1")).calendarWithName("calendar_1")
        toHome = yield self.transactionUnderTest().calendarHomeWithUID(
            "new-home", create=True)
        toCalendar = yield toHome.calendarWithName("calendar")
        yield _migrateCalendar(fromCalendar, toCalendar,
                               lambda x: x.component())
        yield self.assertCalendarsSimilar(fromCalendar, toCalendar)


    @inlineCallbacks
    def test_migrateBadCalendarFromFile(self):
        """
        C{_migrateCalendar()} can migrate a file-backed calendar to a database-
        backed calendar. We need to test what happens when there is "bad" calendar data
        present in the file-backed calendar.
        """
        fromCalendar = yield (yield self.fileTransaction().calendarHomeWithUID(
            "home_bad")).calendarWithName("calendar_bad")
        toHome = yield self.transactionUnderTest().calendarHomeWithUID(
            "new-home", create=True)
        toCalendar = yield toHome.calendarWithName("calendar")
        ok, bad = (yield _migrateCalendar(fromCalendar, toCalendar,
                               lambda x: x.component()))
        self.assertEqual(ok, 1)
        self.assertEqual(bad, 1)


    @inlineCallbacks
    def test_migrateHomeFromFile(self):
        """
        L{migrateHome} will migrate an L{ICalendarHome} provider from one
        backend to another; in this specific case, from the file-based backend
        to the SQL-based backend.
        """
        fromHome = yield self.fileTransaction().calendarHomeWithUID("home1")

        builtinProperties = [PropertyName.fromElement(ResourceType)]

        # Populate an arbitrary / unused dead properties so there's something
        # to verify against.

        key = PropertyName.fromElement(GETContentLanguage)
        fromHome.properties()[key] = GETContentLanguage("C")
        (yield fromHome.calendarWithName("calendar_1")).properties()[key] = (
            GETContentLanguage("pig-latin")
        )
        toHome = yield self.transactionUnderTest().calendarHomeWithUID(
            "new-home", create=True
        )
        yield migrateHome(fromHome, toHome, lambda x: x.component())
        toCalendars = yield toHome.calendars()
        self.assertEquals(set([c.name() for c in toCalendars]),
                          set([k for k in self.requirements['home1'].keys()
                               if self.requirements['home1'][k] is not None]))
        fromCalendars = yield fromHome.calendars()
        for c in fromCalendars:
            self.assertPropertiesSimilar(
                c, (yield toHome.calendarWithName(c.name())),
                builtinProperties
            )
        self.assertPropertiesSimilar(fromHome, toHome, builtinProperties)


    def test_eachCalendarHome(self):
        """
        L{ICalendarStore.eachCalendarHome} is currently stubbed out by
        L{txdav.common.datastore.sql.CommonDataStore}.
        """
        return super(CalendarSQLStorageTests, self).test_eachCalendarHome()


    test_eachCalendarHome.todo = (
        "stubbed out, as migration only needs to go from file->sql currently")


    @inlineCallbacks
    def test_homeProvisioningConcurrency(self):
        """
        Test that two concurrent attempts to provision a calendar home do not
        cause a race-condition whereby the second commit results in a second
        C{INSERT} that violates a unique constraint. Also verify that, while
        the two provisioning attempts are happening and doing various lock
        operations, that we do not block other reads of the table.
        """

        calendarStore = self._sqlCalendarStore

        txn1 = calendarStore.newTransaction()
        txn2 = calendarStore.newTransaction()
        txn3 = calendarStore.newTransaction()

        # Provision one home now - we will use this to later verify we can do
        # reads of existing data in the table
        home_uid2 = yield txn3.homeWithUID(ECALENDARTYPE, "uid2", create=True)
        self.assertNotEqual(home_uid2, None)
        yield txn3.commit()

        home_uid1_1 = yield txn1.homeWithUID(
            ECALENDARTYPE, "uid1", create=True
        )

        @inlineCallbacks
        def _defer_home_uid1_2():
            home_uid1_2 = yield txn2.homeWithUID(
                ECALENDARTYPE, "uid1", create=True
            )
            yield txn2.commit()
            returnValue(home_uid1_2)
        d1 = _defer_home_uid1_2()

        @inlineCallbacks
        def _pause_home_uid1_1():
            yield deferLater(reactor, 1.0, lambda : None)
            yield txn1.commit()
        d2 = _pause_home_uid1_1()

        # Verify that we can still get to the existing home - i.e. the lock
        # on the table allows concurrent reads
        txn4 = calendarStore.newTransaction()
        home_uid2 = yield txn4.homeWithUID(ECALENDARTYPE, "uid2", create=True)
        self.assertNotEqual(home_uid2, None)
        yield txn4.commit()

        # Now do the concurrent provision attempt
        yield d2
        home_uid1_2 = yield d1

        self.assertNotEqual(home_uid1_1, None)
        self.assertNotEqual(home_uid1_2, None)


    @inlineCallbacks
    def test_putConcurrency(self):
        """
        Test that two concurrent attempts to PUT different calendar object
        resources to the same address book home does not cause a deadlock.
        """

        calendarStore = self._sqlCalendarStore

        # Provision the home and calendar now
        txn = calendarStore.newTransaction()
        home = yield txn.homeWithUID(ECALENDARTYPE, "uid1", create=True)
        self.assertNotEqual(home, None)
        cal = yield home.calendarWithName("calendar")
        self.assertNotEqual(cal, None)
        yield txn.commit()

        txn1 = calendarStore.newTransaction()
        txn2 = calendarStore.newTransaction()

        home1 = yield txn1.homeWithUID(ECALENDARTYPE, "uid1", create=True)
        home2 = yield txn2.homeWithUID(ECALENDARTYPE, "uid1", create=True)

        cal1 = yield home1.calendarWithName("calendar")
        cal2 = yield home2.calendarWithName("calendar")

        @inlineCallbacks
        def _defer1():
            yield cal1.createObjectResourceWithName("1.ics", VComponent.fromString(
    "BEGIN:VCALENDAR\r\n"
      "VERSION:2.0\r\n"
      "PRODID:-//Apple Inc.//iCal 4.0.1//EN\r\n"
      "CALSCALE:GREGORIAN\r\n"
      "BEGIN:VTIMEZONE\r\n"
        "TZID:US/Pacific\r\n"
        "BEGIN:DAYLIGHT\r\n"
          "TZOFFSETFROM:-0800\r\n"
          "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU\r\n"
          "DTSTART:20070311T020000\r\n"
          "TZNAME:PDT\r\n"
          "TZOFFSETTO:-0700\r\n"
        "END:DAYLIGHT\r\n"
        "BEGIN:STANDARD\r\n"
          "TZOFFSETFROM:-0700\r\n"
          "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU\r\n"
          "DTSTART:20071104T020000\r\n"
          "TZNAME:PST\r\n"
          "TZOFFSETTO:-0800\r\n"
        "END:STANDARD\r\n"
      "END:VTIMEZONE\r\n"
      "BEGIN:VEVENT\r\n"
        "CREATED:20100203T013849Z\r\n"
        "UID:uid1\r\n"
        "DTEND;TZID=US/Pacific:20100207T173000\r\n"
        "TRANSP:OPAQUE\r\n"
        "SUMMARY:New Event\r\n"
        "DTSTART;TZID=US/Pacific:20100207T170000\r\n"
        "DTSTAMP:20100203T013909Z\r\n"
        "SEQUENCE:3\r\n"
        "BEGIN:VALARM\r\n"
          "X-WR-ALARMUID:1377CCC7-F85C-4610-8583-9513D4B364E1\r\n"
          "TRIGGER:-PT20M\r\n"
          "ATTACH;VALUE=URI:Basso\r\n"
          "ACTION:AUDIO\r\n"
        "END:VALARM\r\n"
      "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
            ))
            yield txn1.commit()
        d1 = _defer1()

        @inlineCallbacks
        def _defer2():
            yield cal2.createObjectResourceWithName("2.ics", VComponent.fromString(
    "BEGIN:VCALENDAR\r\n"
      "VERSION:2.0\r\n"
      "PRODID:-//Apple Inc.//iCal 4.0.1//EN\r\n"
      "CALSCALE:GREGORIAN\r\n"
      "BEGIN:VTIMEZONE\r\n"
        "TZID:US/Pacific\r\n"
        "BEGIN:DAYLIGHT\r\n"
          "TZOFFSETFROM:-0800\r\n"
          "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU\r\n"
          "DTSTART:20070311T020000\r\n"
          "TZNAME:PDT\r\n"
          "TZOFFSETTO:-0700\r\n"
        "END:DAYLIGHT\r\n"
        "BEGIN:STANDARD\r\n"
          "TZOFFSETFROM:-0700\r\n"
          "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU\r\n"
          "DTSTART:20071104T020000\r\n"
          "TZNAME:PST\r\n"
          "TZOFFSETTO:-0800\r\n"
        "END:STANDARD\r\n"
      "END:VTIMEZONE\r\n"
      "BEGIN:VEVENT\r\n"
        "CREATED:20100203T013849Z\r\n"
        "UID:uid2\r\n"
        "DTEND;TZID=US/Pacific:20100207T173000\r\n"
        "TRANSP:OPAQUE\r\n"
        "SUMMARY:New Event\r\n"
        "DTSTART;TZID=US/Pacific:20100207T170000\r\n"
        "DTSTAMP:20100203T013909Z\r\n"
        "SEQUENCE:3\r\n"
        "BEGIN:VALARM\r\n"
          "X-WR-ALARMUID:1377CCC7-F85C-4610-8583-9513D4B364E1\r\n"
          "TRIGGER:-PT20M\r\n"
          "ATTACH;VALUE=URI:Basso\r\n"
          "ACTION:AUDIO\r\n"
        "END:VALARM\r\n"
      "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
            ))
            yield txn2.commit()
        d2 = _defer2()

        yield d1
        yield d2

    @inlineCallbacks
    def test_datetimes(self):
        calendarStore = self._sqlCalendarStore

        # Provision the home and calendar now
        txn = calendarStore.newTransaction()
        home = yield txn.homeWithUID(ECALENDARTYPE, "uid1", create=True)
        cal = yield home.calendarWithName("calendar")
        cal._created = "2011-02-05 11:22:47"
        cal._modified = "2011-02-06 11:22:47"
        self.assertEqual(cal.created(), datetimeMktime(datetime.datetime(2011, 2, 5, 11, 22, 47)))
        self.assertEqual(cal.modified(), datetimeMktime(datetime.datetime(2011, 2, 6, 11, 22, 47)))

        obj = yield self.calendarObjectUnderTest()
        obj._created = "2011-02-07 11:22:47"
        obj._modified = "2011-02-08 11:22:47"
        self.assertEqual(obj.created(), datetimeMktime(datetime.datetime(2011, 2, 7, 11, 22, 47)))
        self.assertEqual(obj.modified(), datetimeMktime(datetime.datetime(2011, 2, 8, 11, 22, 47)))

    @inlineCallbacks
    def test_notificationsProvisioningConcurrency(self):
        """
        Test that two concurrent attempts to provision a notifications collection do not
        cause a race-condition whereby the second commit results in a second
        C{INSERT} that violates a unique constraint.
        """

        calendarStore = self._sqlCalendarStore

        txn1 = calendarStore.newTransaction()
        txn2 = calendarStore.newTransaction()

        notification_uid1_1 = yield txn1.notificationsWithUID(
           "uid1",
        )

        @inlineCallbacks
        def _defer_notification_uid1_2():
            notification_uid1_2 = yield txn2.notificationsWithUID(
                "uid1",
            )
            yield txn2.commit()
            returnValue(notification_uid1_2)
        d1 = _defer_notification_uid1_2()

        @inlineCallbacks
        def _pause_notification_uid1_1():
            yield deferLater(reactor, 1.0, lambda : None)
            yield txn1.commit()
        d2 = _pause_notification_uid1_1()

        # Now do the concurrent provision attempt
        yield d2
        notification_uid1_2 = yield d1

        self.assertNotEqual(notification_uid1_1, None)
        self.assertNotEqual(notification_uid1_2, None)

    @inlineCallbacks
    def test_removeCalendarPropertiesOnDelete(self):
        """
        L{ICalendarHome.removeCalendarWithName} removes a calendar that already
        exists and makes sure properties are also removed.
        """

        # Create calendar and add a property
        home = yield self.homeUnderTest()
        name = "remove-me"
        calendar = yield home.createCalendarWithName(name)
        resourceID = calendar._resourceID
        calendarProperties = calendar.properties()
        
        prop = caldavxml.CalendarDescription.fromString("Calendar to be removed")
        calendarProperties[PropertyName.fromElement(prop)] = prop
        yield self.commit()

        prop = schema.RESOURCE_PROPERTY
        _allWithID = Select([prop.NAME, prop.VIEWER_UID, prop.VALUE],
                        From=prop,
                        Where=prop.RESOURCE_ID == Parameter("resourceID"))

        # Check that two properties are present
        home = yield self.homeUnderTest()
        rows = yield _allWithID.on(self.transactionUnderTest(), resourceID=resourceID)
        self.assertEqual(len(tuple(rows)), 2)
        yield self.commit()

        # Remove calendar and check for no properties
        home = yield self.homeUnderTest()
        yield home.removeCalendarWithName(name)
        rows = yield _allWithID.on(self.transactionUnderTest(), resourceID=resourceID)
        self.assertEqual(len(tuple(rows)), 0)
        yield self.commit()

        # Recheck it
        rows = yield _allWithID.on(self.transactionUnderTest(), resourceID=resourceID)
        self.assertEqual(len(tuple(rows)), 0)
        yield self.commit()

    @inlineCallbacks
    def test_removeCalendarObjectPropertiesOnDelete(self):
        """
        L{ICalendarHome.removeCalendarWithName} removes a calendar object that already
        exists and makes sure properties are also removed (which is always the case as right
        now calendar objects never have properties).
        """

        # Create calendar object
        calendar1 = yield self.calendarUnderTest()
        name = "4.ics"
        component = VComponent.fromString(event4_text)
        metadata = {
            "accessMode": "PUBLIC",
            "isScheduleObject": True,
            "scheduleTag": "abc",
            "scheduleEtags": (),
            "hasPrivateComment": False,
        }
        calobject = yield calendar1.createCalendarObjectWithName(name, component, metadata=metadata)
        resourceID = calobject._resourceID

        prop = schema.RESOURCE_PROPERTY
        _allWithID = Select([prop.NAME, prop.VIEWER_UID, prop.VALUE],
                        From=prop,
                        Where=prop.RESOURCE_ID == Parameter("resourceID"))

        # No properties on existing calendar object
        rows = yield _allWithID.on(self.transactionUnderTest(), resourceID=resourceID)
        self.assertEqual(len(tuple(rows)), 0)

        yield self.commit()

        # Remove calendar and check for no properties
        calendar1 = yield self.calendarUnderTest()
        yield calendar1.removeCalendarObjectWithName(name)
        rows = yield _allWithID.on(self.transactionUnderTest(), resourceID=resourceID)
        self.assertEqual(len(tuple(rows)), 0)
        yield self.commit()

        # Recheck it
        rows = yield _allWithID.on(self.transactionUnderTest(), resourceID=resourceID)
        self.assertEqual(len(tuple(rows)), 0)
        yield self.commit()

    @inlineCallbacks
    def test_removeInboxObjectPropertiesOnDelete(self):
        """
        L{ICalendarHome.removeCalendarWithName} removes an inbox calendar object that already
        exists and makes sure properties are also removed. Inbox calendar objects can have properties.
        """

        # Create calendar object and add a property
        home = yield self.homeUnderTest()
        inbox = yield home.createCalendarWithName("inbox")
        
        name = "4.ics"
        component = VComponent.fromString(event4_text)
        metadata = {
            "accessMode": "PUBLIC",
            "isScheduleObject": True,
            "scheduleTag": "abc",
            "scheduleEtags": (),
            "hasPrivateComment": False,
        }
        calobject = yield inbox.createCalendarObjectWithName(name, component, metadata=metadata)
        resourceID = calobject._resourceID
        calobjectProperties = calobject.properties()

        prop = caldavxml.CalendarDescription.fromString("Calendar object to be removed")
        calobjectProperties[PropertyName.fromElement(prop)] = prop
        yield self.commit()

        prop = schema.RESOURCE_PROPERTY
        _allWithID = Select([prop.NAME, prop.VIEWER_UID, prop.VALUE],
                        From=prop,
                        Where=prop.RESOURCE_ID == Parameter("resourceID"))

        # One property exists calendar object
        rows = yield _allWithID.on(self.transactionUnderTest(), resourceID=resourceID)
        self.assertEqual(len(tuple(rows)), 1)

        yield self.commit()

        # Remove calendar object and check for no properties
        home = yield self.homeUnderTest()
        inbox = yield home.calendarWithName("inbox")
        yield inbox.removeCalendarObjectWithName(name)
        rows = yield _allWithID.on(self.transactionUnderTest(), resourceID=resourceID)
        self.assertEqual(len(tuple(rows)), 0)
        yield self.commit()

        # Recheck it
        rows = yield _allWithID.on(self.transactionUnderTest(), resourceID=resourceID)
        self.assertEqual(len(tuple(rows)), 0)
        yield self.commit()

    @inlineCallbacks
    def test_directShareCreateConcurrency(self):
        """
        Test that two concurrent attempts to create a direct shared calendar
        work concurrently without an exception.
        """

        calendarStore = self._sqlCalendarStore

        # Provision the home and calendar now
        txn = calendarStore.newTransaction()
        home = yield txn.homeWithUID(ECALENDARTYPE, "uid1", create=True)
        self.assertNotEqual(home, None)
        cal = yield home.calendarWithName("calendar")
        self.assertNotEqual(cal, None)
        yield txn.commit()

        txn1 = calendarStore.newTransaction()
        txn2 = calendarStore.newTransaction()

        home1 = yield txn1.homeWithUID(ECALENDARTYPE, "uid1", create=True)
        home2 = yield txn2.homeWithUID(ECALENDARTYPE, "uid1", create=True)

        shares1 = yield home1.retrieveOldShares()
        shares2 = yield home2.retrieveOldShares()

        record = SharedCollectionRecord(
            "abcd",
            "D",
            "/calendars/__uids__/uid2/calendar/",
            "XYZ",
            "Shared Wiki Calendar",
        )

        @inlineCallbacks
        def _defer1():
            yield shares1.addOrUpdateRecord(record)
            yield txn1.commit()
        d1 = _defer1()

        @inlineCallbacks
        def _defer2():
            yield shares2.addOrUpdateRecord(record)
            yield txn2.commit()
        d2 = _defer2()

        yield d1
        yield d2

