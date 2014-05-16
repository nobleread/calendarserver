#!/usr/bin/env python
# -*- test-case-name: calendarserver.tools.test.test_agent -*-
##
# Copyright (c) 2013-2014 Apple Inc. All rights reserved.
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
A service spawned on-demand by launchd, meant to handle configuration requests
from Server.app.  When a request comes in on the socket specified in the
launchd agent.plist, launchd will run "caldavd -t Agent" which ends up creating
this service.  Requests are made using HTTP POSTS to /gateway, and are
authenticated by OpenDirectory.
"""

from __future__ import print_function

__all__ = [
    "makeAgentService",
]

import cStringIO
from plistlib import readPlistFromString, writePlistToString
import socket

from twext.python.launchd import getLaunchDSocketFDs
from twext.python.log import Logger
from twext.who.checker import HTTPDigestCredentialChecker
from twext.who.opendirectory import (
    DirectoryService as OpenDirectoryDirectoryService,
    NoQOPDigestCredentialFactory
)
from twisted.application.internet import StreamServerEndpointService
from twisted.cred.portal import IRealm, Portal
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.internet.endpoints import AdoptedStreamServerEndpoint
from twisted.internet.protocol import Factory
from twisted.protocols import amp
from twisted.web.guard import HTTPAuthSessionWrapper
from twisted.web.resource import IResource, Resource, ForbiddenResource
from twisted.web.server import Site, NOT_DONE_YET
from zope.interface import implements


log = Logger()



class AgentRealm(object):
    """
    Only allow a specified list of avatar IDs to access the site
    """
    implements(IRealm)

    def __init__(self, root, allowedAvatarIds):
        """
        @param root: The root resource of the site
        @param allowedAvatarIds: The list of IDs to allow access to
        """
        self.root = root
        self.allowedAvatarIds = allowedAvatarIds


    def requestAvatar(self, avatarId, mind, *interfaces):
        if IResource in interfaces:
            if avatarId.shortNames[0] in self.allowedAvatarIds:
                return (IResource, self.root, lambda: None)
            else:
                return (IResource, ForbiddenResource(), lambda: None)

        raise NotImplementedError()



class AgentGatewayResource(Resource):
    """
    The gateway resource which forwards incoming requests through
    gateway.Runner.
    """
    isLeaf = True

    def __init__(self, store, directory, inactivityDetector):
        """
        @param store: an already opened store
        @param directory: a directory service
        @param inactivityDetector: the InactivityDetector to tell when requests
            come in
        """
        Resource.__init__(self)
        self.store = store
        self.directory = directory
        self.inactivityDetector = inactivityDetector


    def render_POST(self, request):
        """
        Take the body of the POST request and feed it to gateway.Runner();
        return the result as the response body.
        """

        self.inactivityDetector.activity()

        def onSuccess(result, output):
            txt = output.getvalue()
            output.close()
            request.write(txt)
            request.finish()

        def onError(failure):
            message = failure.getErrorMessage()
            tbStringIO = cStringIO.StringIO()
            failure.printTraceback(file=tbStringIO)
            tbString = tbStringIO.getvalue()
            tbStringIO.close()
            error = {
                "Error": message,
                "Traceback": tbString,
            }
            log.error("command failed {error}", error=failure)
            request.write(writePlistToString(error))
            request.finish()

        from calendarserver.tools.gateway import Runner
        body = request.content.read()
        command = readPlistFromString(body)
        output = cStringIO.StringIO()
        runner = Runner(self.store, [command], output=output)
        d = runner.run()
        d.addCallback(onSuccess, output)
        d.addErrback(onError)
        return NOT_DONE_YET



def makeAgentService(store):
    """
    Returns a service which will process GatewayAMPCommands, using a socket
    file descripter acquired by launchd

    @param store: an already opened store
    @returns: service
    """
    from twisted.internet import reactor

    sockets = getLaunchDSocketFDs()
    fd = sockets["AgentSocket"][0]

    family = socket.AF_INET
    endpoint = AdoptedStreamServerEndpoint(reactor, fd, family)

    directory = store.directoryService()

    def becameInactive():
        log.warn("Agent inactive; shutting down")
        reactor.stop()

    from twistedcaldav.config import config
    inactivityDetector = InactivityDetector(
        reactor, config.AgentInactivityTimeoutSeconds, becameInactive
    )
    root = Resource()
    root.putChild(
        "gateway",
        AgentGatewayResource(
            store, directory, inactivityDetector
        )
    )

    # We need this service to be able to return com.apple.calendarserver,
    # so tell it not to suppress system accounts.
    directory = OpenDirectoryDirectoryService(
        "/Local/Default", suppressSystemRecords=False
    )

    portal = Portal(
        AgentRealm(root, [u"com.apple.calendarserver"]),
        [HTTPDigestCredentialChecker(directory)]
    )
    credentialFactory = NoQOPDigestCredentialFactory(
        "md5", "/Local/Default"
    )
    wrapper = HTTPAuthSessionWrapper(portal, [credentialFactory])

    site = Site(wrapper)

    return StreamServerEndpointService(endpoint, site)



class InactivityDetector(object):
    """
    If no 'activity' takes place for a specified amount of time, a method
    will get called.  Activity causes the inactivity time threshold to be
    reset.
    """

    def __init__(self, reactor, timeoutSeconds, becameInactive):
        """
        @param reactor: the reactor
        @timeoutSeconds: the number of seconds considered to mean inactive
        @becameInactive: the method to call (with no arguments) when
            inactivity is reached
        """
        self._reactor = reactor
        self._timeoutSeconds = timeoutSeconds
        self._becameInactive = becameInactive

        if self._timeoutSeconds > 0:
            self._delayedCall = self._reactor.callLater(
                self._timeoutSeconds,
                self._inactivityThresholdReached
            )


    def _inactivityThresholdReached(self):
        """
        The delayed call has fired.  We're inactive.  Call the becameInactive
            method.
        """
        self._becameInactive()


    def activity(self):
        """
        Call this to let the InactivityMonitor that there has been activity.
        It will reset the timeout.
        """
        if self._timeoutSeconds > 0:
            if self._delayedCall.active():
                self._delayedCall.reset(self._timeoutSeconds)
            else:
                self._delayedCall = self._reactor.callLater(
                    self._timeoutSeconds,
                    self._inactivityThresholdReached
                )


    def stop(self):
        """
        Cancels the delayed call
        """
        if self._timeoutSeconds > 0:
            if self._delayedCall.active():
                self._delayedCall.cancel()



#
# Alternate implementation using AMP instead of HTTP
#

class GatewayAMPCommand(amp.Command):
    """
    A command to be executed by gateway.Runner
    """
    arguments = [('command', amp.String())]
    response = [('result', amp.String())]



class GatewayAMPProtocol(amp.AMP):
    """
    Passes commands to gateway.Runner and returns the results
    """

    def __init__(self, store, directory):
        """
        @param store: an already opened store
            operations
        @param directory: a directory service
        """
        amp.AMP.__init__(self)
        self.store = store
        self.directory = directory


    @GatewayAMPCommand.responder
    @inlineCallbacks
    def gatewayCommandReceived(self, command):
        """
        Process a command via gateway.Runner

        @param command: GatewayAMPCommand
        @returns: a deferred returning a dict
        """
        command = readPlistFromString(command)
        output = cStringIO.StringIO()
        from calendarserver.tools.gateway import Runner
        runner = Runner(
            self.store,
            [command], output=output
        )

        try:
            yield runner.run()
            result = output.getvalue()
            output.close()
        except Exception as e:
            error = {"Error": str(e)}
            result = writePlistToString(error)

        output.close()
        returnValue(dict(result=result))



class GatewayAMPFactory(Factory):
    """
    Builds GatewayAMPProtocols
    """
    protocol = GatewayAMPProtocol

    def __init__(self, store):
        """
        @param store: an already opened store
        """
        self.store = store
        self.directory = self.store.directoryService()


    def buildProtocol(self, addr):
        return GatewayAMPProtocol(
            self.store, self.davRootResource, self.directory
        )



#
# A test AMP client
#

command = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
        <key>command</key>
        <string>getLocationAndResourceList</string>
</dict>
</plist>"""


def getList():
    # For the sample client, below:
    from twisted.internet import reactor
    from twisted.internet.protocol import ClientCreator

    creator = ClientCreator(reactor, amp.AMP)
    host = '127.0.0.1'
    import sys
    if len(sys.argv) > 1:
        host = sys.argv[1]
    d = creator.connectTCP(host, 62308)

    def connected(ampProto):
        return ampProto.callRemote(GatewayAMPCommand, command=command)
    d.addCallback(connected)

    def resulted(result):
        return result['result']
    d.addCallback(resulted)

    def done(result):
        print('Done: %s' % (result,))
        reactor.stop()
    d.addCallback(done)
    reactor.run()

if __name__ == '__main__':
    getList()
