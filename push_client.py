from idigi_ws_api import Api, RestResource
from threading import Thread
from xml.dom.minidom import parseString
import socket, ssl, pprint, struct, time
import json
import select
import zlib
import logging
from Queue import Queue, Empty

log = logging.getLogger(__name__)

import httplib, urllib
from base64 import encodestring
from xml.dom.minidom import getDOMImplementation, parseString, Element
import uuid, re

import warnings

# Push Opcodes.
CONNECTION_REQUEST = 0x01
CONNECTION_RESPONSE = 0x02
PUBLISH_MESSAGE = 0x03
PUBLISH_MESSAGE_RECEIVED = 0x04

# Possible Responses from iDigi with respect to Push.
STATUS_OK = 200
STATUS_UNAUTHORIZED = 403
STATUS_BAD_REQUEST = 400

# Ports to Connect on for Push.
PUSH_OPEN_PORT = 3200
PUSH_SECURE_PORT = 3201

class PushException(Exception):
    """
    Indicates an issue interacting with iDigi Push Functionality.
    """
    pass

class PushSession(object):
    """
    A PushSession is responsible for establishing a socket connection
    with iDigi to receive events generated by Devices connected to 
    iDigi.
    """
    
    def __init__(self, callback, monitor_id, client):
        """
        Creates a PushSession for use with interacting with iDigi's
        Push Functionality.
        
        Arguments:
        callback   -- The callback function to invoke when data is received.  
                      Must have 1 required parameter that will contain the
                      payload.
        monitor_id -- The id of the Monitor to observe.
        client     -- The client object this session is derived from.
        """
        self.callback   = callback
        self.monitor_id = monitor_id
        self.client     = client
        self.socket     = None
        
    def send_connection_request(self):
        """
        Sends a ConnectionRequest to the iDigi server using the credentials
        established with the id of the monitor as defined in the monitor 
        member.
        """
        try:
            log.info("Sending ConnectionRequest for Monitor %s." 
                % self.monitor_id)
            # Send connection request and perform a receive to ensure
            # request is authenticated.
            # Protocol Version = 1.
            payload  = struct.pack('!H', 0x01)
            # Username Length.
            payload += struct.pack('!H', len(self.client.username))
            # Username.
            payload += self.client.username
            # Password Length.
            payload += struct.pack('!H', len(self.client.password))
            # Password.
            payload += self.client.password
            # Monitor ID.
            payload += struct.pack('!L', int(self.monitor_id))

            # Header 6 Bytes : Type [2 bytes] & Length [4 Bytes]
            # ConnectionRequest is Type 0x01.
            data = struct.pack("!HL", CONNECTION_REQUEST, len(payload))

            # The full payload.
            data += payload

            # Send Connection Request.
            self.socket.send(data)

            # Set a 10 second blocking on recv, if we don't get any data
            # within 10 seconds, timeout which will throw an exception.
            self.socket.settimeout(10)

            # Should receive 10 bytes with ConnectionResponse.
            response = self.socket.recv(10)

            # Make socket blocking.
            self.socket.settimeout(0)

            if len(response) != 10:
                raise PushException("Length of Connection Request Response \
(%d) is not 10." % len(response))

            # Type
            response_type = int(struct.unpack("!H", response[0:2])[0])
            if response_type != CONNECTION_RESPONSE:
                raise PushException("Connection Response Type (%d) is not \
ConnectionResponse Type (%d)." % (response_type, CONNECTION_RESPONSE))

            status_code = struct.unpack("!H", response[6:8])[0]
            log.info("Got ConnectionResponse for Monitor %s with Status %s." 
                % (self.monitor_id, status_code))
            if status_code != STATUS_OK:
                raise PushException("Connection Response Status Code (%d) is \
not STATUS_OK (%d)." % STATUS_OK)
        except Exception, e:
            self.socket.close()
            self.socket = None
            raise e

    def start(self):
        """
        Creates a TCP connection to the iDigi Server and sends a 
        ConnectionRequest message.
        """
        log.info("Starting Insecure Session for Monitor %s." % self.monitor_id)
        if self.socket is not None:
            raise Exception("Socket already established for %s." % self)
        
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.connect((self.client.hostname, PUSH_OPEN_PORT))
            self.socket.setblocking(0)
        except Exception, e:
            self.socket.close()
            self.socket = None
            raise e
        
        self.send_connection_request()
            
    def stop(self):
        """
        Closes the socket associated with this session and puts Session 
        into a state such that it can be re-established later.
        """
        self.socket.close()
        self.socket = None

class SecurePushSession(PushSession):
    """
    SecurePushSession extends PushSession by wrapping the socket connection
    in SSL.  It expects the certificate to match any of those in the passed
    in ca_certs member file.
    """
    
    def __init__(self, callback, monitor_id, client, ca_certs=None):
        """
        Creates a PushSession wrapped in SSL for use with interacting with 
        iDigi's Push Functionality.
        
        Arguments:
        callback   -- The callback function to invoke when data is received.  
                      Must have 1 required parameter that will contain the
                      payload.
        monitor_id -- The id of the Monitor to observe.
        client     -- The client object this session is derived from.
        
        Keyword Arguments:
        ca_certs -- Path to a file containing Certificates.  If not None,
                    iDigi Server must present a certificate present in the 
                    ca_certs file.
        """
        PushSession.__init__(self, callback, monitor_id, client)
        self.ca_certs = ca_certs
    
    def start(self):
        """
        Creates a SSL connection to the iDigi Server and sends a 
        ConnectionRequest message.
        """
        log.info("Starting SSL Session for Monitor %s." % self.monitor_id)
        if self.socket is not None:
            raise Exception("Socket already established for %s." % self)
        
        try:
            # Create socket, wrap in SSL and connect.
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # Validate that certificate server uses matches what we expect.
            if self.ca_certs is not None:
                self.socket = ssl.wrap_socket(self.socket, 
                                                cert_reqs=ssl.CERT_REQUIRED, 
                                                ca_certs=self.ca_certs)
            else:
                self.socket = ssl.wrap_socket(self.socket)

            # TODO (possibly) for some reason python can't parse the peer
            # cert.  It would be really nice to assert that the hostname
            # matches what we expect.
            self.socket.connect((self.client.hostname, PUSH_SECURE_PORT))
            self.socket.setblocking(0)
        except Exception, e:
            self.socket.close()
            self.socket = None
            raise e
            
        self.send_connection_request()

class CallbackWorkerPool(object):
    """
    A Worker Pool implementation that creates a number of predefined threads
    used for invoking Session callbacks.
    """

    def __consume_queue(self):
        while True:
            session, block_id, data = self.__queue.get()
            try:
                if session.callback(data):
                    # Send a Successful PublishMessageReceived with the 
                    # block id sent in request
                    if self.__write_queue is not None:
                        response_message = struct.pack('!HHH', 
                                            PUBLISH_MESSAGE_RECEIVED, 
                                            block_id, 200)
                        self.__write_queue.put((session.socket, 
                            response_message))
            except Exception, e:
                log.exception(e)

            self.__queue.task_done()


    def __init__(self, write_queue=None, size=20):
        """
        Creates a Callback Worker Pool for use in invoking Session Callbacks 
        when data is received by a push client.

        Keyword Arguments:
        write_queue -- Queue used for queueing up socket write events for when 
                       a payload message is received and processed.
        size        -- The number of worker threads to invoke callbacks.
        """
        # Used to queue up PublishMessageReceived events to be sent back to 
        # the iDigi server.
        self.__write_queue = write_queue
        # Used to queue up sessions and data to callback with.
        self.__queue = Queue(size)
        # Number of workers to create.
        self.size = size

        for _ in range(size): 
            worker = Thread(target=self.__consume_queue)
            worker.daemon = True
            worker.start()

    def queue_callback(self, session, block_id, data):
        """
        Queues up a callback event to occur for a session with the given 
        payload data.  Will block if the queue is full.

        Keyword Arguments:
        session  -- the session with a defined callback function to call.
        block_id -- the block_id of the message received.
        data     -- the data payload of the message received.
        """
        self.__queue.put((session, block_id, data))        

class PushClient(object):
    """
    A Client for the 'Push' feature in iDigi.
    """
    
    def __init__(self, username, password, hostname='developer.idigi.com', 
                secure=True, ca_certs=None):
        """
        Creates a Push Client for use in creating monitors and creating sessions 
        for them.
        
        Arguments:
        username -- Username of user in iDigi to authenticate with.
        password -- Password of user in iDigi to authenticate with.
        
        Keyword Arguments:
        hostname -- Hostname of iDigi server to connect to.
        secure   -- Whether or not to create a secure SSL wrapped session.
        ca_certs -- Path to a file containing Certificates.  If not None,
                    iDigi Server must present a certificate present in the 
                    ca_certs file if 'secure' is specified to True.
        """
        self.api      = Api(username, password, hostname)
        self.hostname = hostname
        self.username = username
        self.password = password
        self.secure   = secure
        self.ca_certs = ca_certs
        
        # A dict mapping Sockets to their PushSessions
        self.sessions          = {}
        # IO thread is used monitor sockets and consume data.
        self.__io_thread       = None
        # Writer thread is used to send data on sockets.
        self.__writer_thread   = None
        # Write queue is used to queue up data to write to sockets.
        self.__write_queue     = Queue()
        # A pool that monitors callback events and invokes them.
        self.__callback_pool   = CallbackWorkerPool(self.__write_queue)

        self.closed            = False


    def create_monitor(self, topics, batch_size=1, batch_duration=0, 
        compression='none', format_type='xml'):
        """
        Creates a Monitor instance in iDigi for a given list of topics.
        
        Arguments:
        topics -- a string list of topics (i.e. ['DeviceCore[U]', 
                  'FileDataCore']).
        
        Keyword Arguments:
        batch_size     -- How many Msgs received before sending data.
        batch_duration -- How long to wait before sending batch if it does not 
                          exceed batch_size.
        compression    -- Compression value (i.e. 'gzip').
        format_type    -- What format server should send data in (i.e. 'xml' or 
                          'json').
        
        Returns a Monitor RestResource object.
        """
        
        # Create RestResource and POST it.
        monitor = RestResource.create('Monitor', monTopic=','.join(topics), 
                                        monBatchSize=str(batch_size),
                                        monBatchDuration=str(batch_duration),
                                        monFormatType=format_type,
                                        monTransportType='tcp',
                                        monCompression=compression)
        location = self.api.post(monitor)
        
        # Perform a GET by Id to get all Monitor data.
        monitor = self.api.get_first(location)
        # Set the location so it can be used for future reference.
        monitor.location = location
        return monitor
    
    def delete_monitor(self, monitor):
        """
        Attempts to Delete a Monitor from iDigi.  Throws exception if 
        Monitor does not exist.
        
        Arguments:
        monitor -- RestResource representing the monitor to delete.
        """
        self.api.delete(monitor)
        
    def get_monitor(self, topics):
        """
        Attempts to find a Monitor in iDigi that matches the input list of 
        topics.
        
        Arguments:
        topics -- a string list of topics 
                  (i.e. ['DeviceCore[U]', 'FileDataCore']).
        
        Returns a RestResource Monitor instance if match found, otherwise None.
        """
        # Query for Monitor conditionally by monTopic.
        monitor = self.api.get_first('Monitor', 
            condition="monTopic='%s'" % ','.join(topics))
        if monitor is not None:
            monitor.location = 'Monitor/%s' % monitor.monId
        return monitor
        
    def __restart_session(self, session):
        """
        Restarts and re-establishes session.

        Arguments:
        session -- The session to restart.
        """
        log.info("Attempting restart session for Monitor Id %s."
             % session.monitor_id)
        # remove old session key
        del self.sessions[session.socket.fileno()]
        session.stop()
        session.start()
        self.sessions[session.socket.fileno()] = session

    def __writer(self):
        """
        Indefinitely checks the writer queue for data to write
        to socket.
        """
        while not self.closed:
            try:
                socket, data = self.__write_queue.get(timeout=0.1)
                socket.send(data)
                self.__write_queue.task_done()
            except Empty,e:
                pass # nothing to write after timeout

    def __select(self):
        """
        While the client is not marked as closed, performs a socket select
        on all PushSession sockets.  If any data is received, parses and
        forwards it on to the callback function.  If the callback is 
        successful, a PublishMessageReceived message is sent.
        """
        try:
            while not self.closed:
                try:
                    # TODO if any of the descriptors become bad
                    # (This can happen if session is stopped) remove
                    # it from sessions.
                    inputready, outputready, exceptready =\
                        select.select(self.sessions.keys(), [], [], 0.1)
                    for sock in inputready:
                        session = self.sessions[sock]
                        sck = session.socket
                        
                        # Read header information before receiving rest of
                        # message.
                        data = sck.recv(6)
                        
                        # check for socket close event
                        if len(data) == 0:
                            if not self.sessions.has_key(session):
                                # Session no longer tracked, dont restart.
                                continue
                            # Attempt to Restart Session
                            log.error("Socket closed for Monitor %s." 
                                % session.monitor_id)
                            log.warn("Restarting Session \
for Monitor %s." % session.monitor_id)
                            self.__restart_session(session) 
                            continue

                        response_type = struct.unpack('!H', data[0:2])[0]
                        message_length = struct.unpack('!i', data[2:6])[0]
                        
                        if response_type != PUBLISH_MESSAGE:
                            log.warn("Response Type (%x) does not match \
PublishMessageReceived (%x)" % (response_type, PUBLISH_MESSAGE))
                            continue

                        data = sck.recv(message_length)
                        while len(data) < message_length :
                             time.sleep(1)
                             data = data + sck.recv(message_length - len(data))

                        block_id = struct.unpack('!H', data[0:2])[0]
                        aggregate_count = struct.unpack('!H', data[2:4])[0]
                        compression = struct.unpack('!B', data[4:5])[0]
                        format = struct.unpack('!B', data[5:6])[0]
                        payload_size = struct.unpack('!i', data[6:10])
                        payload = data[10:]

                        if compression == 0x01:
                            # Data is compressed, uncompress it.
                            # Note, this doesn't work yet.
                            payload = zlib.decompress(payload)
                        
                        # Enqueue payload into a callback queue to be
                        # invoked.
                        self.__callback_pool.queue_callback(session, 
                            block_id, payload)
                        
                except Exception, ex:
                    log.exception(ex)

        finally:
            for session in self.sessions.values():
                session.stop()
    
    def __init_threads(self):
        # This is the first session, start the io_thread
        if self.__io_thread is None:
            self.__io_thread = Thread(target=self.__select)
            self.__io_thread.start()

        if self.__writer_thread is None:
            self.__writer_thread = Thread(target=self.__writer)
            self.__writer_thread.start()

           
    def create_session(self, callback, monitor=None, monitor_id=None):
        """
        Creates and Returns a PushSession instance based on the input monitor
        and callback.  When data is received, callback will be invoked.
        If neither monitor or monitor_id are specified, throws an Exception.
        
        Arguments:
        callback -- Callback function to call when PublishMessage messages are
                    received. Expects 1 argument which will contain the 
                    payload of the pushed message.  Additionally, expects 
                    function to return True if callback was able to process 
                    the message, False or None otherwise.
        
        Keyword Arguments:
        monitor    -- Monitor RestResource instance that will be registered on.
        monitor_id -- The id of the Monitor as id knows it, will be queried 
                      to understand parameters of the monitor.
        """
        if monitor is None and monitor_id is None:
            raise PushException('monitor or monitor_id must be provided.')

        # If monitor_id is not set, monitor must have been set, get it's monId.
        mon_id = monitor_id
        if mon_id is None:
            mon_id = monitor.monId

        log.info("Creating Session for Monitor %s." % mon_id)
        session = SecurePushSession(callback, mon_id, self, self.ca_certs) \
            if self.secure else PushSession(callback, mon_id, self)

        session.start()
        self.sessions[session.socket.fileno()] = session
        
        self.__init_threads()
        return session
    
    def stop_all(self):
        """
        Stops all session activity.  Blocks until io and writer thread dies.
        """
        if self.__io_thread is not None:
            self.closed = True
            
            while self.__io_thread.is_alive():
                time.sleep(1)

        if self.__writer_thread is not None:
            self.closed = True

            while self.__writer_thread.is_alive():
                time.sleep(1)

def json_cb(data):
    """
    Sample callback, parses data as json and pretty prints it.
    Returns True if json is valid, False otherwise.
    
    Arguments:
    data -- The payload of the PublishMessage.
    """
    try:
        json_data = json.loads(data)
        log.info("Data Received: %s" % (json.dumps(json_data, sort_keys=True, 
                                        indent=4)))
        return True
    except Exception, e:
        print e
    return False

def xml_cb(data):
    """
    Sample callback, parses data as xml and pretty prints it.
    Returns True if xml is valid, False otherwise.

    Arguments:
    data -- The payload of the PublishMessage.
    """
    try:
        dom = parseString(data)
        print "Data Received: %s" % (dom.toprettyxml())

        return True
    except Exception, e:
        print e
    
    return False
        
if __name__ == "__main__":
    logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', 
                    datefmt='%m/%d/%Y %I:%M:%S %p', level=logging.INFO)

    import argparse
    parser = argparse.ArgumentParser(description="iDigi Push Client Sample", 
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    
    parser.add_argument('username', type=str,
        help='Username to authenticate with.')

    parser.add_argument('password', type=str,
        help='Password to authenticate with.')

    parser.add_argument('--topics', '-t', dest='topics', action='store', 
        type=str, default='DeviceCore', 
        help='A comma-separated list of topics to listen on.')

    parser.add_argument('--host', '-a', dest='host', action='store', 
        type=str, default='test.idigi.com', 
        help='iDigi server to connect to.')

    parser.add_argument('--ca_certs', dest='ca_certs', action='store',
        type=str,
        help='File containing iDigi certificates to authenticate server with.')

    parser.add_argument('--insecure', dest='secure', action='store_false',
        default=True,
        help='Prevent client from making secure (SSL) connection.')

    parser.add_argument('--compression', dest='compression', action='store',
        type=str, default='none', choices=['none', 'gzip'],
        help='Compression type to use.')

    parser.add_argument('--format', dest='format', action='store',
        type=str, default='json', choices=['json', 'xml'],
        help='Format data should be pushed up in.')

    parser.add_argument('--batchsize', dest='batchsize', action='store',
        type=int, default=1,
        help='Amount of messages to batch up before sending data.')

    parser.add_argument('--batchduration', dest='batchduration', action='store',
        type=int, default=60,
        help='Number of seconds to wait before sending batch if \
batchsize not met.')
        
    args = parser.parse_args()
    log.info("Creating Push Client.")
    client = PushClient(args.username, args.password, hostname=args.host,
                        secure=args.secure, ca_certs=args.ca_certs)
    
    topics = args.topics.split(',')

    log.info("Checking to see if Monitor Already Exists.")
    monitor = client.get_monitor(topics)

    # Delete Monitor if it Exists.
    if monitor is not None:
        log.info("Monitor already exists, deleting it.")
        client.delete_monitor(monitor)
    
    monitor = client.create_monitor(topics, format_type=args.format,
        compression=args.compression, batch_size=args.batchsize, 
        batch_duration=args.batchduration)

    try:
        callback = json_cb if args.format=="json" else xml_cb
        session = client.create_session(callback, monitor)
        while True:
            time.sleep(3.14)
    except KeyboardInterrupt:
        # Expect KeyboardInterrupt (CTRL+C or CTRL+D) and print friendly msg.
        log.warn("Keyboard Interrupt Received.  \
Closing Sessions and Cleaning Up.")
    finally:
        client.stop_all()
        client.delete_monitor(monitor)
