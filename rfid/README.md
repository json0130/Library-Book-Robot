# RFID Sensing Application

Application for reading RFID tags on library books from an Arduino and sending book IDs over PySerial.

## Usage

1. Install Arduino RFID [library](https://github.com/miguelbalboa/rfid)

2. Connect and upload `rfid_write.ino` script to Arduino

3. Run `rfid_read.py` script to read library book IDs from Python

Note: make sure to close the Arduino IDE after uploading to allow the Python script to use the port