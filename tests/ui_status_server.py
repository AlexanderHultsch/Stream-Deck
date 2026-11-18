import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    source = server.DryRunProcessSource()
    httpd = server.create_server(root, "127.0.0.1", 0, process_source=source)
    watcher = httpd.watcher
    watcher.interval = 0.3
    watcher.burst_interval = 0.1
    watcher.burst_seconds = 3.0
    print("PORT %d" % httpd.server_address[1], flush=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        for line in sys.stdin:
            parts = line.split()
            if not parts:
                continue
            command = parts[0]
            if command == "quit":
                break
            if command == "start" and len(parts) == 2:
                source.processes.append((parts[1].lower(), 1000 + len(source.processes)))
            elif command == "stop" and len(parts) == 2:
                wanted = parts[1].lower()
                source.processes[:] = [p for p in source.processes if p[0] != wanted]
            elif command == "state":
                print("WATCHER %d SUBS %d" % (watcher.thread is not None, len(watcher.subscribers)),
                      flush=True)
    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
