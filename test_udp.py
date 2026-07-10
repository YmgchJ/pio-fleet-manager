import socket

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(('0.0.0.0', 5000))
sock.settimeout(10.0)

try:
    while True:
        data, addr = sock.recvfrom(1024)
        print(f"Received from {addr}: {data.decode('utf-8')}")
except Exception as e:
    print("Timeout or error:", e)
finally:
    sock.close()
