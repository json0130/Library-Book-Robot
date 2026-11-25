import serial
import time

ser = serial.Serial(port='/dev/ttyACM0', baudrate=9600)

while True:
    x = ser.readline()
    print(x)