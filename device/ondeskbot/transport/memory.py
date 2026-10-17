"""单元测试和桌面模拟使用的内存 Transport。"""


class MemoryTransport:
    name = "memory"

    def __init__(self):
        self.identity = None
        self.connected = False
        self.commands = []
        self.responses = []
        self.events = []
        self.heartbeats = []
        self.artifacts = []

    def connect(self, identity):
        self.identity = identity
        self.connected = True
        return True

    def is_connected(self):
        return self.connected

    def enqueue(self, command):
        self.commands.append(command)

    def poll_command(self):
        return self.commands.pop(0) if self.commands else None

    def publish_response(self, response):
        self.responses.append(response)
        return True

    def publish_event(self, event):
        self.events.append(event)
        return True

    def heartbeat(self, payload):
        self.heartbeats.append(payload)
        return True

    def upload(self, data, content_type, metadata, filename="artifact.bin"):
        artifact_id = "mem-{:06d}".format(len(self.artifacts) + 1)
        self.artifacts.append({
            "artifact_id": artifact_id,
            "filename": filename,
            "content_type": content_type,
            "metadata": metadata,
            "data": bytes(data),
        })
        return {"artifact_id": artifact_id, "uri": "memory://" + artifact_id,
                "content_type": content_type}

    def close(self):
        self.connected = False
