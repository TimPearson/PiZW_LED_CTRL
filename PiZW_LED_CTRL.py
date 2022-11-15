import logging
import socket
import select
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
"""

if platform.machine() == 'armv6l':
    import RPi.GPIO as GPIO

    log_fn = '/home/pi/logst.log'
    THIS_IS_A_PI = True
    HOST = 'SIGCNTRL.local'
    #HOST = 'CougarUb.local'
else:
    import dummyGPIO as GPIO

    log_fn = '/home/tim/logst.log'
    HOST = 'CougarUb.local'
    THIS_IS_A_PI = False

PORT = 65433  # The port used by the server
VERSION = 3.81

"""
Normal Operation
----------------
In normal operation it will firstly initialise all of the LEDs to output and off
It will try and connect to a 'server' which will be the main RPi4 (running Tim's signalling app)
Then it will start up LEDs in a defined sequence with flickering for a while where specified
It will then run until it receives a shutdown command from the server at which point the unit will
shutdown

The power up sequence will allow several leds to be turned on at once - this is controlled by the relevant tuple of tuples
e.g. nth_power_up_order_tuple = ((25,26), (17,18) , (20,)) - Each of the inner tuples indicates what group of leds to be started
together

VERSION 0.4 - original working version written in November 2021
VERSION 1.0 - Revised version to allow reconnection if Main Signalling Pi Server Drops connection - May 22
VERSION 2.0 - Rewrite for UDP replacing TCP
VERSION 3.0 - Rewrite to see if can get to work as a systemctl service unit
Version 3.72 - Working pretty well and deployed to insert REQ/ACK logic
VERSION 3.81 - Changed log level

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

MIN_FLICKER_TIME = 5
MAX_FLICKER_TIME = 500

TESTING = True  # if true then on an end command only return to the command line
# otherwise shutdown the pi

# global variables
lock = None  # A lock used to ensure access to led i/o library is not re-entered
shutdown_flag = False  # A flag to signal form the comms thread to the main thread that its time to go
exitFlag = False  # A flag to indicate to all threads when we need to quit
node = platform.node()


logging.basicConfig(format='%(asctime)s - %(message)s', filename=log_fn, level=logging.WARNING)
logging.info('================')
logging.warning('top of the world v%.2f', VERSION)
if THIS_IS_A_PI:
    logging.warning('Its a pi!')
else:
    logging.info('NOT a pi')
logging.info('platform node name: %s', node)


class ClientComms:
    """ A class to managed polled network comms with the light/signal controller
        intended to be instantiated only once

    """

    def __init__(self):
        self.host = HOST
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except:
            logging.info('socket error')
        else:
            logging.info('no socket error')
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except:
            logging.info('socket options error')
        else:
            logging.info('no socket options error')

        self.sock.bind(('', PORT))
        self.connected = False
        self.last_stay_alive_packet_time = millis()
        self.send_packet_count = 0
        self.last_message = 'Hello client' + str(VERSION)
        self.reqflag = False
        self.reqcount = 0

    def stay_alive(self):
        probe_string = self.last_message  # + str(self.packet_count)
        self.send_packet_count += 1
        self.last_stay_alive_packet_time = millis()
        if self.reqflag:
            self.reqflag = False
        try:
            self.sock.sendto(probe_string.encode(), (HOST, PORT))
        except:
            logging.info('stay alive send fail')
        else:
            logging.info('stay alive send ok')

    def poll_comms(self):
        # This looks for incoming commands, takes them form the input socket and actions them
        # returns a bool which indicates if a req is needed

        global shutdown_flag
        global exitFlag     # Flag to say stop flickering please
        logging.info('polling')

        input_ready, output_ready, except_ready = select.select([0, self.sock], [], [], 0)
        for i in input_ready:
            if i == self.sock:
                data, addr = i.recvfrom(1024)
                logging.info('receiving')
                self.last_message = data.decode()
                if data:
                    logging.info('got data')
                    print(data.decode())
                    logging.info(data.decode())

                    if data.decode() == "REQ":
                        self.reqflag = True
                        self.reqcount += 1
                        print("req received")


                    if data.decode() == "ON":
                        exitFlag = True
                        print("All leds switched on")
                        leds_on()

                    elif data.decode() == "OFF":
                        print("All leds switched off")
                        exitFlag = True
                        leds_off()

                    elif data.decode() == "END":
                        print("shutdown_message")
                        exitFlag = True
                        shutdown_flag = True


                    else:
                        msg_string = data.decode()
                        length = len(msg_string)
                        print("received message", msg_string)
                        if msg_string[0:7] == "ILED_ON" and 7 < length < 10:
                            try:
                                gpio = int(msg_string[7: length])
                                print("gpio is:", gpio)
                                specific_led_on(gpio)
                            except ValueError:
                                print("message not well formed")
                        elif msg_string[0:8] == "ILED_OFF" and 8 < length < 11:
                            try:
                                gpio = int(msg_string[8: length])
                                print("gpio is:", gpio)
                                specific_led_off(gpio)
                            except ValueError:
                                print("message not well formed")
                        elif msg_string[0:7] == "HLED_ON" and length == 9:
                            header = int(msg_string[7])
                            pin = int(msg_string[8])
                            print("hled on message", header, pin)
                            specific_led_on(convert_to_gpio(header, pin))
                        elif msg_string[0:8] == "HLED_OFF" and length == 10:
                            header = int(msg_string[8])
                            pin = int(msg_string[9])
                            print("hled off message", header, pin)
                            specific_led_off(convert_to_gpio(header, pin))

        return self.reqflag

    def close(self):
        self.sock.close()


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
        print("starting Thread runtime is", self.runtime_millis / 1000, "seconds")
        self.process_data()
        print("exiting Thread ")

    def process_data(self):
        while not exitFlag:
            random_limits = int(blinkOn_list[self.blinkOn_index] / 2)
            random_delta = random.randint(-1 * random_limits, random_limits)
            time.sleep((random_delta + random_limits * 2) / 1000)
            self.blinkOn_index += 1
            # print("flickering")
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
    print("initialising leds")
    if THIS_IS_A_PI:
        GPIO.setmode(GPIO.BCM)
    for i in led_list:
        GPIO.setup(i, GPIO.OUT)
        GPIO.output(i, GPIO.LOW)
    lock.release()

    return


def leds_on():
    # Turn all leds on immediately - no flicker or any sh*t like that
    print("***ON routine***")
    global lock
    lock.acquire()
    for i in led_list:
        # print('led on:', i)
        GPIO.output(i, GPIO.HIGH)
        # pass
    lock.release()
    return


def leds_on_scenic():
    global power_up_order_tuple
    global power_up_delays
    global lock
    logging.info('leds on scenic')
    # print("leds on in scenic sequence")

    delay_index = 0
    delay = 0

    for current_tuple in power_up_order_tuple:
        delay = power_up_delays[delay_index]
        delay_index += 1
        for led in current_tuple:
            # iterate through the power up order tuple, and either light the led or start a flicker thread for it
            if led in flicker_leds:
                print("flicker led", led)

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
    print("***OFF_routine***")
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
        # print("specific led on", led_gpio)
        GPIO.output(led_gpio, GPIO.HIGH)
    lock.release()


def specific_led_off(led_gpio):
    # turn off a specific led ignoring any flicker
    global lock
    if led_gpio in led_list:
        lock.acquire()
        # print("specific led off", led_gpio)
        GPIO.output(led_gpio, GPIO.LOW)
        lock.release()


def convert_to_gpio(h, p):
    """ given a header number (h) and a pin number (p) generate a bcm gpio pin number"""
    print("BCM gpio is", mk2_led_list[headers_list[h - 1] + p - 1])
    return mk2_led_list[headers_list[h - 1] + p - 1]


def leds_close():
    global lock
    lock.acquire()
    print('cleanup')
    GPIO.cleanup()
    lock.release()


def configure_board():
    """ based on the board hostname configure the board to its known interface board version
    return a boolean to show if this has worked or not
    """
    global headers_list
    global power_up_order_tuple
    global led_list
    global flicker_leds
    global power_up_delays

    hostname = socket.gethostname()

    if THIS_IS_A_PI:

        if hostname.endswith("nth"):
            print("its north")
            headers_list = mk2_headers_start_index
            power_up_order_tuple = nth_power_up_order_tuple
            led_list = mk2_led_list
            flicker_leds = nth_flicker_leds
            power_up_delays = nth_power_up_delays

            return True
        elif hostname.endswith('sth'):  # 'sth'"EHL01"):
            print("its south")
            headers_list = mk1_headers_start_index
            power_up_order_tuple = sth_power_up_order_tuple
            led_list = mk1_led_list
            flicker_leds = sth_flicker_leds
            power_up_delays = sth_power_up_delays
            return True
        else:
            print("oh dear me - board unknown")
            return False
    return True



def main():
    print("Pi Zero LED controller v", VERSION)
    global lock
    global exitFlag
    global shutdown_flag

    # instantiate the lock for accessing led i/o
    lock = threading.Lock()

    my_client = ClientComms()
    if not configure_board():
        my_client.close()
        logging.warning('Unknown Board')
        exit()
    # configure all the led channels to output and set them all to off
    leds_init()

    #Then configure to leds on scenic
    leds_on_scenic()
    #leds_on()
    my_client.stay_alive()

    while True:
        if my_client.poll_comms() or millis() - my_client.last_stay_alive_packet_time > 1000:
            my_client.stay_alive()

        time.sleep(0.2)
        if shutdown_flag:
            print("got shutdown")
            break
    exitFlag = True
    time.sleep(2)
    my_client.close()
    leds_close()
    if shutdown_flag:
        print("shutdown flag received")
        if TESTING:
            print("testing so just exit to command line")
        elif THIS_IS_A_PI:
            logging.info('ending in shutdown')
            time.sleep(1)
            print("trying to shut the pi")
            command = "/usr/bin/sudo /sbin/shutdown -h now"
            import subprocess
            process = subprocess.Popen(command.split(), stdout=subprocess.PIPE)

    logging.info('ending normally...')


if __name__ == '__main__':
    main()
