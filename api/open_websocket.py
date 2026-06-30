import websocket #NOTE: websocket-client (https://github.com/websocket-client/websocket-client)
import uuid

from api.config import get_comfyui_host


def open_websocket_connection():
  server_address = get_comfyui_host()
  client_id = str(uuid.uuid4())

  ws = websocket.WebSocket()
  ws.connect("ws://{}/ws?clientId={}".format(server_address, client_id))
  return ws, server_address, client_id
