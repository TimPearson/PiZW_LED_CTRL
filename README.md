# PiZW_LED_CTRL

Code to run the LEDs connected to the LED HAT PCB. Part of the Station Lighting Project

Overview:

If you connect LEDs to the PiZW_LED_HAT (see my repository for that PCB) then this code can be used to control the LEDs and communicate with the signal controller.

This code is poorly written and tied to my setup so feel free to use as a basis but it's not neat and ready to go.

The facilities offered by this code are:

UDP comms to and from the controller
Startup LEDs after boot
Flicker LEDs as required
Shutdown the whole unit as required from the controller.

I auto start this on the Pi Zero with the following systemd service:/


[Unit]\
Description=LED control service/
After=multi-user.target/
/
[Service]/
ExecStart=/usr/bin/python3 /home/pi/PIZW_LED_CTRL.py/
User=root/
/
[Install]/
WantedBy=multi-user.target/

