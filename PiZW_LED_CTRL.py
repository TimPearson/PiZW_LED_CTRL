import logging
import socket
import select
import sys
import time
import platform
import threading
import random

"""
Rpi ZERO led code - which is used as a slave to the signal controller

This code can be run on an intel machine for test and development or on a RPizero
If its an intel test and development machine then look use dummyGPIO instead of a real one
Also if running on Intel assume the cougar machine is the host, if not assume it is
the actual signal controller

The comms code uses UDP - so it doesn't have to have a TCP session open for hours at a time
It is multi-threaded to achieve the flickering effect, but the comms is simple polling.

"""

if platform.machine() == 'armv6l':
    import RPi.GPIO as GPIO

    log_fn = '/home/pi/logst.log'
    this_is_a_pi = True
    HOST = 'SIGCNTRL.local'
else:
    import dummy_gpio as GPIO

    log_fn = '/home/tim/logst.log'
    HOST = 'CougarUb.local'
    this_is_a_pi = False

PORT = 65433  # The port used by the server
VERSION = 3.90
STAY_ALIVE_INTERVAL_IN_MS = 1000

"""
Normal Operation
----------------
In normal operation it will firstly initialise all of the LEDs to output and off
It will try and connect to a 'server' which will be the main RPi4 (running Tim's signalling app)
Then it will start up LEDs in a defined sequence with flickering for a while where specified
It will then run until it receives a shutdown command from the server at which point the unit will
shutdown

The power up sequence will allow several leds to be turned on at once - this is controlled by the relevant tuple of 
tuples e.g. nth_power_up_order_tuple = ((25,26), (17,18) , (20,)) - Each of the inner tuples indicates what group of
leds to be started together

VERSION 0.4 - original working version written in November 2021
VERSION 1.0 - Revised version to allow reconnection if Main Signalling Pi Server Drops connection - May 22
VERSION 2.0 - Rewrite for UDP replacing TCP
VERSION 3.0 - Rewrite to see if can get to work as a systemctl service unit
Version 3.72 - Working pretty well and deployed to insert REQ/ACK logic
VERSION 3.81 - Changed log level
Version 3.90 - Improved comms code with some tuning, some bug fixes and stat recording

Notes on the Hardware interface boards
--------------------------------------
For mk2 boards there are four 10 way headers, J1-J4
Headers have a maximum of 8 signal pins, pins 9 and 10 being +Ve rails (The signal pins
are the returns to GND (depending on the pin state))

21 pins in total
J1 and J2 are duplicated signals.
J4 has only 5 signals (pins 1-5)
J1&J2 are GPIOs(BCM labelled) 
25,26,4,5,6,7,8,9
J3 are GPIOS(BCM labelled)
10,11,12,13,14,15,16,17
J4 are GPIOS(BCM labelled)
18,19,20,21,22

Notes on the comms protocol
--------------------------------------
Basics of the comms protocol - it's loose UDP so packets likely often get lost
Once this client end starts it sends stay alive packets every 1 second (see STAY_ALIVE_INTERVAL_IN_MS)
It starts off sending a HELLO + Version message, Once it receives a message from the server - it changes the
stay alive packet contents to the last message sent. The only messages sent to the server are stay alive packets

The server however can send a variety of commands to this client - specifically it can say:
LEDs ON, LEDs OFF or shutdown. (other commands such as individual led commands are possible).

The server can also send a REQ packet if it hasn't received a stay alive recently - this just means send me
a new stay alive as soon as possible.

All packets in each direction end with a ">>>" followed by a packet count as a string. 
each direction has it's own count.

"""

# The mk1 board has a different set of header pins for led control to the mk2 these lists give the order
# of the gpio pins as they are connected to the headers
mk1_led_list = [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22]
mk2_led_list = [25, 26, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22]

# these lists show where each header from the board starts as an index in the above lists
mk1_headers_start_index = [0, 0, 8, 15]
mk2_headers_start_index = [0, 0, 8, 16]

thread_list = []

nth_power_up_order_tuple = ((25, 26), (17, 18), (20,), (4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 19, 21, 22))
# need one delay for each sub-tuple in the power up order tuple
nth_power_up_delays = (1000, 1000, 1000, 1000, 1000)
nth_flicker_leds = (17,)
sth_power_up_order_tuple = ((22, 21), (17, 18), (20,), (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 19))
sth_flicker_leds = (17,)
# need one delay for each sub-tuple in the power up order tuple
sth_power_up_delays = (1000, 1000, 1000, 1000, 1000)

blinkOn_list = [10, 20, 20, 240, 20, 40, 20, 100, 20, 20, 20, 260, 80, 20, 240, 60, 160, 20, 240, 20, 1000, 20, 20, 40,
                100, 20, 2740, 340, 860, 20, 1400, 20, 60, 20]
# all_gpio_list = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 25, 26]

led_list = []  # filled in by configure routine
headers_list = []  # filled in by configure routine
power_up_order_tuple = ()  # filled in by configure routine
flicker_leds = ()  # filled in by configure routine
power_up_delays = ()  # filled in by configure routine

# ======================================================================================================
# Some constants
MIN_FLICKER_TIME = 5
MAX_FLICKER_TIME = 500
TESTING = False  # if true then on an end command only return to the command line
# otherwise shutdown the pi

# ======================================================================================================
# global variables

lock = None  # A lock used to ensure access to led i/o library is not re-entered
shutdown_flag = False  # A flag to signal form the comms thread to the main thread that it's time to go
exitFlag = False  # A flag to indicate to all threads when we need to quit
node = platform.node()

# ======================================================================================================
# Logging setup
FILE_LOG_LEVEL = logging.INFO
CONSOLE_LOG_LEVEL = logging.INFO
fmt = '%(asctime)s.%(msecs)03d - %(funcName)s - %(levelname)s - %(message)s'
date_fmt = '%H:%M:%S'
logging.basicConfig(datefmt=date_fmt, format=fmt,
                    filename=log_fn, level=FILE_LOG_LEVEL)
logger = logging.getLogger('LightLog')
logger.setLevel(logging.INFO)
con_log = logging.StreamHandler(sys.stderr)
con_log.setLevel(level=CONSOLE_LOG_LEVEL)
con_log_formatter = logging.Formatter(fmt, date_fmt)
con_log.setFormatter(con_log_formatter)
logger.addHandler(con_log)

logger.warning('lights log starting v%.2f', VERSION)
if this_is_a_pi:
    logger.warning('Its a pi!')
else:
    logger.info('NOT a pi')
logger.info('platform node name: %s', node)


# ======================================================================================================

class ClientComms:
    """ A class to managed polled network comms with the light/signal controller
        intended to be instantiated only once
    """

    def __init__(self):
        self.host = HOST
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except:
            logger.info('socket error')
        else:
            logger.info('no socket error')
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except:
            logger.info('socket options error')

        self.sock.bind(('', PORT))

        self.last_stay_alive_packet_time = millis()
        self.send_packet_count = 0  # both the number of sent packets and the packet index to add
        self.last_message = 'Hello client ' + str(VERSION)  # store what was last received, repeat back on next send
        self.req_flag = False  # true if the last packet received was a REQ packet
        self.req_packets_count = 0  # record total number of req packets here
        self.last_rx_packet_index = None  # The last packet index received (None or int)
        self.good_sequence = 0  # count of packets received in order
        # self.rx_packet_count = 0            # count of total packets received
        self.lost_rx_packets = 0  # count of missing RX packets
        self.restarts = 0  # count of the number of times packets received without increasing index
        self.total_rx_packet_count = 0  # count of total received packets

    def send_stay_alive_packet(self):
        """
        Send Stay Alive packet
        :return: None
        """
        probe_string = self.last_message + '>>>' + str(self.send_packet_count)
        self.send_packet_count += 1
        self.last_stay_alive_packet_time = millis()
        if self.req_flag:
            self.req_flag = False
        try:
            self.sock.sendto(probe_string.encode(), (HOST, PORT))
        except:
            logger.info('stay alive send fail')
        else:
            logger.info(f'stay alive send ok: {probe_string}')
        return

    def poll_comms(self) -> bool:
        """This looks for incoming commands, takes them from the input socket and actions them
        returns a bool which indicates if the server is REQuesting a stay alive as that will be handled outside this
        function
        """

        global shutdown_flag
        global exitFlag  # Flag to say stop flickering please
        # logger.info('polling')

        input_ready, output_ready, except_ready = select.select([0, self.sock], [], [], 0)
        for i in input_ready:
            if i == self.sock:
                data, (ip, port) = i.recvfrom(1024)
                packet = data.decode('utf-8')
                logger.info('receiving')
                msg_string = self.strip_packet_tail(packet)

                if data:
                    logger.info(f'got data{msg_string}')

                    if msg_string == "REQ":
                        self.req_flag = True
                        self.req_packets_count += 1
                        logger.info("req received")

                    if msg_string == "ON":
                        exitFlag = True
                        logger.info("All leds switched on")
                        leds_on()

                    elif msg_string == "OFF":
                        logger.info("All leds switched off")
                        exitFlag = True
                        leds_off()

                    elif msg_string == "END":
                        logger.info("shutdown_message")
                        exitFlag = True
                        shutdown_flag = True

                    else:

                        length = len(msg_string)
                        if msg_string[0:7] == "ILED_ON" and 7 < length < 10:
                            try:
                                gpio = int(msg_string[7: length])
                                logger.info("gpio is:", gpio)
                                specific_led_on(gpio)
                            except ValueError:
                                logger.info("message not well formed")
                        elif msg_string[0:8] == "ILED_OFF" and 8 < length < 11:
                            try:
                                gpio = int(msg_string[8: length])
                                logger.info("gpio is:", gpio)
                                specific_led_off(gpio)
                            except ValueError:
                                logger.warning("message not well formed")
                        elif msg_string[0:7] == "HLED_ON" and length == 9:
                            header = int(msg_string[7])
                            pin = int(msg_string[8])
                            logger.info("hled on message", header, pin)
                            specific_led_on(convert_to_gpio(header, pin))
                        elif msg_string[0:8] == "HLED_OFF" and length == 10:
                            header = int(msg_string[8])
                            pin = int(msg_string[9])
                            logger.info("hled off message", header, pin)
                            specific_led_off(convert_to_gpio(header, pin))

        return self.req_flag

    def close(self):
        logger.critical(f'Closing comms with stats: '
                        f'sent {self.send_packet_count},'
                        f'rxed {self.total_rx_packet_count},'
                        f'good seq {self.good_sequence},'
                        f'reqcount {self.req_packets_count},'
                        f'lost rx {self.lost_rx_packets},'
                        f'restarts of server {self.restarts}')
        self.sock.close()

    def strip_packet_tail(self, decoded_packet: str) -> str:
        """The packet should finish with >>>number_string - so find the >>> extract number and check it
        then return the string without the tail
        """
        tail_pos = decoded_packet.rfind('>>>')
        if tail_pos == -1:
            logger.error('no packet count on incoming packet')
            trunc_pkt = decoded_packet
        else:
            self.total_rx_packet_count += 1
            trunc_pkt = decoded_packet[:tail_pos]
            index = int(decoded_packet[tail_pos + 3:])
            if trunc_pkt != "REQ":
                self.last_message = trunc_pkt
            prev_index = self.last_rx_packet_index
            self.last_rx_packet_index = index
            if prev_index:
                if index - prev_index == 1:
                    self.good_sequence += 1
                else:
                    if index > prev_index:
                        self.lost_rx_packets += index - (prev_index + 1)
                    else:
                        self.restarts = 0
            else:
                self.good_sequence += 1

        return trunc_pkt


# =========================================================================================================


class FlickerThread(threading.Thread):
    """ A class to start and operate a new thread to cope with a single led flickering startup
    an instance is called for each led that starts with fluorescent tube flicker once the flicker
    sequence is finished the thread is closed.
    """

    def __init__(self, led_gpio, runtime_secs):
        threading.Thread.__init__(self)
        # self.threadID = thread_id
        # self.lock = gpio_lock
        self.gpio = led_gpio
        self.blinkOn_index = random.randint(0, len(blinkOn_list) - 1)
        if runtime_secs == 0:
            self.run_forever = True
        else:
            self.run_forever = False
            self.runtime_millis = runtime_secs * 1000
        self.start_time = None
        self.end_time = None
        self.last_on = False

    def run(self):
        self.start_time = millis()
        self.end_time = self.start_time + self.runtime_millis
        logger.info("starting Thread runtime is", self.runtime_millis / 1000, "seconds")
        self.process_data()
        logger.info("exiting Thread ")

    def process_data(self):
        while not exitFlag:
            random_limits = int(blinkOn_list[self.blinkOn_index] / 2)
            random_delta = random.randint(-1 * random_limits, random_limits)
            time.sleep((random_delta + random_limits * 2) + 0.01 / 1000)
            self.blinkOn_index += 1
            if self.blinkOn_index == len(blinkOn_list):
                self.blinkOn_index = 0

            if not self.run_forever and self.end_time < millis():
                # make the last thing you do to leave led on
                specific_led_on(self.gpio)
                break

            if self.last_on:
                self.last_on = False
                specific_led_off(self.gpio)
            else:
                self.last_on = True
                specific_led_on(self.gpio)


def millis():
    return int(round(time.time() * 1000))


def leds_init():
    # initialise all led outputs to output mode and to off
    global lock
    lock.acquire()
    logger.info("initialising leds")
    if this_is_a_pi:
        GPIO.setmode(GPIO.BCM)
    for i in led_list:
        GPIO.setup(i, GPIO.OUT)
        GPIO.output(i, GPIO.LOW)
    lock.release()

    return


def leds_on():
    # Turn all leds on immediately - no flicker or any sh*t like that
    logger.info("***ON routine***")
    global lock
    lock.acquire()
    for i in led_list:
        GPIO.output(i, GPIO.HIGH)
        # pass
    lock.release()
    return


def leds_on_scenic():
    global power_up_order_tuple
    global power_up_delays
    global lock
    logger.info('leds on scenic')

    delay_index = 0
    delay = 0

    for current_tuple in power_up_order_tuple:
        delay = power_up_delays[delay_index]
        delay_index += 1
        for led in current_tuple:
            # iterate through the power up order tuple, and either light the LED or start a flicker thread for it
            if led in flicker_leds:

                athread = (FlickerThread(led_gpio=led, runtime_secs=random.randint(MIN_FLICKER_TIME, MAX_FLICKER_TIME)))
                athread.start()
            else:
                lock.acquire()
                GPIO.output(led, GPIO.HIGH)

                lock.release()
        time.sleep(delay / 1000)


def leds_off():
    # Turn all leds off immediately (assumes that all flickering has ended)
    global lock
    logger.info("***OFF_routine***")
    lock.acquire()
    for i in led_list:
        GPIO.output(i, GPIO.LOW)
    lock.release()
    return


def specific_led_on(led_gpio):
    # turn on a specific led ignoring any flicker requests
    global lock
    lock.acquire()
    if led_gpio in led_list:
        logger.info("specific led on", led_gpio)
        GPIO.output(led_gpio, GPIO.HIGH)
    lock.release()


def specific_led_off(led_gpio):
    # turn off a specific led ignoring any flicker
    global lock
    if led_gpio in led_list:
        lock.acquire()
        logger.info("specific led off", led_gpio)
        GPIO.output(led_gpio, GPIO.LOW)
        lock.release()


def convert_to_gpio(h, p):
    """ given a header number (h) and a pin number (p) generate a bcm gpio pin number"""
    # print("BCM gpio is", mk2_led_list[headers_list[h - 1] + p - 1])
    return mk2_led_list[headers_list[h - 1] + p - 1]


def leds_close():
    global lock
    lock.acquire()
    leds_off()
    GPIO.cleanup()
    lock.release()


def configure_board() -> bool:
    """ based on the board hostname configure the board to its known interface board version
    return a boolean to show if this has worked or not
    """
    global headers_list
    global power_up_order_tuple
    global led_list
    global flicker_leds
    global power_up_delays

    hostname = socket.gethostname()

    if this_is_a_pi:

        if hostname.endswith("nth"):
            logger.critical("+++ NORTH UNIT +++")
            headers_list = mk2_headers_start_index
            power_up_order_tuple = nth_power_up_order_tuple
            led_list = mk2_led_list
            flicker_leds = nth_flicker_leds
            power_up_delays = nth_power_up_delays

            return True
        elif hostname.endswith('sth'):  # 'sth'"EHL01"):
            logger.critical("+++ SOUTH UNIT +++")
            headers_list = mk1_headers_start_index
            power_up_order_tuple = sth_power_up_order_tuple
            led_list = mk1_led_list
            flicker_leds = sth_flicker_leds
            power_up_delays = sth_power_up_delays
            return True
        else:
            logger.critical("+++board unknown+++")
            return False
    return True


def main():
    global lock
    global exitFlag
    global shutdown_flag

    # instantiate the lock for accessing led i/o - to stop the flicker thread and the main thread
    # from both accessing LEDs at the same time
    lock = threading.Lock()

    # Start the comms to the server
    my_client = ClientComms()

    # configure the I/O board values depending on hostname
    if not configure_board():
        my_client.close()
        exit()

    # configure all the LED channels to output and set them all to off
    leds_init()

    # Then configure to leds on scenic
    leds_on_scenic()

    # send the first stay alive packet
    my_client.send_stay_alive_packet()

    # This is the main loop, send stay alives and sleep until get a shutdown flay
    while True:

        got_a_req = my_client.poll_comms()
        if got_a_req or millis() - my_client.last_stay_alive_packet_time > STAY_ALIVE_INTERVAL_IN_MS:
            my_client.send_stay_alive_packet()

        if got_a_req:
            time.sleep(0.05)
        else:
            time.sleep(0.1)

        if shutdown_flag:
            logger.info("got shutdown")
            break

    # Turn off the comms
    my_client.close()

    # Tell the flicker thread to shutdown and wait for it to do so.
    exitFlag = True
    time.sleep(2)

    # Turn off the LEDs
    leds_close()

    # if this is a PI and we aren't testing then shutdown the whole thing
    if shutdown_flag and this_is_a_pi and not TESTING:
        logger.info('ending in shutdown')
        time.sleep(1)
        command = "/usr/bin/sudo /sbin/shutdown -h now"
        import subprocess
        process = subprocess.Popen(command.split(), stdout=subprocess.PIPE)

    logger.info('ending without shutdown...')


if __name__ == '__main__':
    logger.critical(f"Zero Comms Simulator {VERSION}")
    main()
