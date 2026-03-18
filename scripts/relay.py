import os
import socket
import sys
import threading

BACKEND_HOST = os.environ.get("DOCKER_BRIDGE_IP", "172.18.0.1")
BIND_HOST = os.environ.get("RELAY_BIND_IP", "10.200.0.1")
VLLM_PORT = int(os.environ.get("VLLM_PORT", "8000"))


def relay(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    finally:
        try:
            src.close()
        except Exception:
            pass
        try:
            dst.close()
        except Exception:
            pass


def handle(client):
    try:
        backend = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        backend.connect((BACKEND_HOST, VLLM_PORT))
        t1 = threading.Thread(target=relay, args=(client, backend), daemon=True)
        t2 = threading.Thread(target=relay, args=(backend, client), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    except Exception as e:
        print(f"Connection error: {e}", file=sys.stderr)
        client.close()


server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind((BIND_HOST, VLLM_PORT))
server.listen(32)
print(f"Relay listening on {BIND_HOST}:{VLLM_PORT} -> {BACKEND_HOST}:{VLLM_PORT}", flush=True)
while True:
    client, addr = server.accept()
    print(f"Connection from {addr}", flush=True)
    threading.Thread(target=handle, args=(client,), daemon=True).start()
