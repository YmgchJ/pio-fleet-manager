import serial
import serial.tools.list_ports
import time

ports = list(serial.tools.list_ports.comports())
target_port = None
for p in ports:
    if "usbmodem" in p.device or "USB" in p.description:
        target_port = p.device
        break

if not target_port:
    print("No Pico port found.")
    exit(1)

print(f"Opening {target_port}...")
try:
    ser = serial.Serial(target_port, 115200, timeout=1.0)
    print("Port opened. Listening for 5 seconds...")
    start_time = time.time()
    while time.time() - start_time < 5.0:
        if ser.in_waiting:
            line = ser.readline().decode('utf-8', errors='ignore')
            print(f"RX: {line.strip()}")
        else:
            time.sleep(0.1)
    ser.close()
    print("Done listening.")
except Exception as e:
    print(f"Error: {e}")
