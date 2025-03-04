import json
import pickle
import pprint
import re
import ssl
import threading
import time
import traceback
import uuid
import random

import cloudscraper
import paho.mqtt.client as mqtt
import requests
import requests.adapters

from enum import IntEnum
from http.cookiejar import LWPCookieJar

from .constant import (
    AUTH_FINISH_PATH,
    AUTH_GET_FACTORID,
    AUTH_GET_FACTORS,
    AUTH_PATH,
    AUTH_START_PAIRING,
    AUTH_START_PATH,
    AUTH_VALIDATE_PATH,
    DEFAULT_RESOURCES,
    DEVICES_PATH,
    LOGOUT_PATH,
    MQTT_HOST,
    MQTT_PATH,
    MQTT_URL_KEY,
    NOTIFY_PATH,
    ORIGIN_HOST,
    REFERER_HOST,
    SESSION_PATH,
    SUBSCRIBE_PATH,
    TFA_CONSOLE_SOURCE,
    TFA_IMAP_SOURCE,
    TFA_PUSH_SOURCE,
    TFA_REST_API_SOURCE,
    TRANSID_PREFIX,
    USER_AGENTS,
)
from .sseclient import SSEClient
from .tfa import Arlo2FAConsole, Arlo2FAImap, Arlo2FARestAPI
from .util import days_until, now_strftime, time_to_arlotime, to_b64


class AuthResult(IntEnum):
    CAN_RETRY = -1,
    SUCCESS = 0,
    FAILED = 1


# include token and session details
class ArloBackEnd(object):

    _session_lock = threading.Lock()
    _session_info = {}
    _multi_location = False
    _user_device_id = None
    _browser_auth_code = None
    _user_id: str | None = None
    _web_id: str | None = None
    _sub_id: str | None = None
    _token: str | None = None
    _expires_in: int | None = None
    _needs_pairing: bool = False

    def __init__(self, arlo):

        self._arlo = arlo
        self._lock = threading.Condition()
        self._req_lock = threading.Lock()

        self._dump_file = self._arlo.cfg.dump_file
        self._use_mqtt = False

        self._requests = {}
        self._callbacks = {}
        self._resource_types = DEFAULT_RESOURCES

        self._load_session()
        if self._user_device_id is None:
            self._arlo.debug("created new user ID")
            self._user_device_id = str(uuid.uuid4())

        # event thread stuff
        self._event_thread = None
        self._event_client = None
        self._event_connected = False
        self._stop_thread = False

        # login
        self._session = None
        self._load_cookies()
        self._logged_in = self._login()
        if not self._logged_in:
            self.debug("failed to log in")
            return

    def _load_session(self):
        self._user_id = None
        self._web_id = None
        self._sub_id = None
        self._token = None
        self._expires_in = 0
        self._browser_auth_code = None
        self._user_device_id = None
        if not self._arlo.cfg.save_session:
            return
        try:
            with ArloBackEnd._session_lock:
                with open(self._arlo.cfg.session_file, "rb") as dump:
                    ArloBackEnd._session_info = pickle.load(dump)
                    version = ArloBackEnd._session_info.get("version", 1)
                    if version == "2":
                        session_info = ArloBackEnd._session_info.get(self._arlo.cfg.username, None)
                    else:
                        session_info = ArloBackEnd._session_info
                        ArloBackEnd._session_info = {
                            "version": "2",
                            self._arlo.cfg.username: session_info,
                        }
                    if session_info is not None:
                        self._user_id = session_info["user_id"]
                        self._web_id = session_info["web_id"]
                        self._sub_id = session_info["sub_id"]
                        self._token = session_info["token"]
                        self._expires_in = session_info["expires_in"]
                        if "browser_auth_code" in session_info:
                            self._browser_auth_code = session_info["browser_auth_code"]
                        if "device_id" in session_info:
                            self._user_device_id = session_info["device_id"]
                        self.debug(f"loadv{version}:session_info={ArloBackEnd._session_info}")
                    else:
                        self.debug(f"loadv{version}:failed")
        except Exception:
            self.debug("session file not read")
            ArloBackEnd._session_info = {
                "version": "2",
            }

    def _save_session(self):
        if not self._arlo.cfg.save_session:
            return
        try:
            with ArloBackEnd._session_lock:
                with open(self._arlo.cfg.session_file, "wb") as dump:
                    ArloBackEnd._session_info[self._arlo.cfg.username] = {
                        "user_id": self._user_id,
                        "web_id": self._web_id,
                        "sub_id": self._sub_id,
                        "token": self._token,
                        "expires_in": self._expires_in,
                        "browser_auth_code": self._browser_auth_code,
                        "device_id": self._user_device_id,
                    }
                    pickle.dump(ArloBackEnd._session_info, dump)
                    self.debug(f"savev2:session_info={ArloBackEnd._session_info}")
        except Exception as e:
            self._arlo.warning("session file not written" + str(e))

    def _save_cookies(self, requests_cookiejar):
        if self._cookies is not None:
            self.debug(f"saving-cookies={self._cookies}")
            self._cookies.save(ignore_discard=True)

    def _load_cookies(self):
        self._cookies = LWPCookieJar(self._arlo.cfg.cookies_file)
        try:
            self._cookies.load()
        except:
            pass
        self.debug(f"loading cookies={self._cookies}")

    def _transaction_id(self):
        return 'FE!' + str(uuid.uuid4())

    def _build_url(self, url, tid):
        sep = "&" if "?" in url else "?"
        now = time_to_arlotime()
        return f"{url}{sep}eventId={tid}&time={now}"

    def _request_tuple(
            self,
            path,
            method="GET",
            params=None,
            headers=None,
            stream=False,
            raw=False,
            timeout=None,
            host=None,
            authpost=False,
            cookies=None
    ):
        if params is None:
            params = {}
        if headers is None:
            headers = {}
        if timeout is None:
            timeout = self._arlo.cfg.request_timeout
        try:
            with self._req_lock:
                if host is None:
                    host = self._arlo.cfg.host
                if authpost:
                    url = host + path
                else:
                    tid = self._transaction_id()
                    url = self._build_url(host + path, tid)
                    headers['x-transaction-id'] = tid

                self.vdebug("request-url={}".format(url))
                self.vdebug("request-params=\n{}".format(pprint.pformat(params)))
                self.vdebug("request-headers=\n{}".format(pprint.pformat(headers)))

                if method == "GET":
                    r = self._session.get(
                        url,
                        params=params,
                        headers=headers,
                        stream=stream,
                        timeout=timeout,
                        cookies=cookies,
                    )
                    if stream is True:
                        return 200, r
                elif method == "PUT":
                    r = self._session.put(
                        url, json=params, headers=headers, timeout=timeout, cookies=cookies,
                    )
                elif method == "POST":
                    r = self._session.post(
                        url, json=params, headers=headers, timeout=timeout, cookies=cookies,
                    )
                elif method == "OPTIONS":
                    self._session.options(
                        url, json=params, headers=headers, timeout=timeout
                    )
                    return 200, None
        except Exception as e:
            self._arlo.warning("request-error={}".format(type(e).__name__))
            return 500, None

        try:
            if "application/json" in r.headers["Content-Type"]:
                body = r.json()
            else:
                body = r.text
            self.vdebug("request-body=\n{}".format(pprint.pformat(body)))
        except Exception as e:
            self._arlo.warning("body-error={}".format(type(e).__name__))
            self._arlo.debug(f"request-text={r.text}")
            return 500, None

        self.vdebug("request-end={}".format(r.status_code))
        if r.status_code != 200:
            return r.status_code, None

        if raw:
            return 200, body

        # New auth style and TFA helper
        if "meta" in body:
            if body["meta"]["code"] == 200:
                return 200, body["data"]
            else:
                # don't warn on untrusted errors, they just mean we need to log in
                if body["meta"]["error"] != 9204:
                    self._arlo.warning("error in new response=" + str(body))
                return int(body["meta"]["code"]), body["meta"]["message"]

        # Original response type
        elif "success" in body:
            if body["success"]:
                if "data" in body:
                    return 200, body["data"]
                # success, but no data so fake empty data
                return 200, {}
            else:
                self._arlo.warning("error in response=" + str(body))

        return 500, None

    def _request(
            self,
            path,
            method="GET",
            params=None,
            headers=None,
            stream=False,
            raw=False,
            timeout=None,
            host=None,
            authpost=False,
            cookies=None
    ):
        code, body = self._request_tuple(path=path, method=method, params=params, headers=headers,
                                         stream=stream, raw=raw, timeout=timeout, host=host, authpost=authpost, cookies=cookies)
        return body

    def gen_trans_id(self, trans_type=TRANSID_PREFIX):
        return trans_type + "!" + str(uuid.uuid4())

    def _event_dispatcher(self, response):

        # get message type(s) and id(s)
        responses = []
        resource = response.get("resource", "")

        err = response.get("error", None)
        if err is not None:
            self._arlo.info(
                "error: code="
                + str(err.get("code", "xxx"))
                + ",message="
                + str(err.get("message", "XXX"))
            )

        #
        # I'm trying to keep this as generic as possible... but it needs some
        # smarts to figure out where to send responses - the packets from Arlo
        # are anything but consistent...
        # See docs/packets for and idea of what we're parsing.
        #

        # Answer for async ping. Note and finish.
        # Packet type #1
        if resource.startswith("subscriptions/"):
            self.vdebug("packet: async ping response " + resource)
            return

        # These is a base station mode response. Find base station ID and
        # forward response.
        # Packet type #2
        if resource == "activeAutomations":
            self.debug("packet: base station mode response")
            for device_id in response:
                if device_id != "resource":
                    responses.append((device_id, resource, response[device_id]))

        # Mode update response
        # XXX these might be deprecated
        elif "states" in response:
            self.debug("packet: mode update")
            device_id = response.get("from", None)
            if device_id is not None:
                responses.append((device_id, "states", response["states"]))

        # These are individual device updates, they are usually used to signal
        # things like motion detection or temperature changes.
        # Packet type #3
        elif [x for x in self._resource_types if resource.startswith(x + "/")]:
            self.debug("packet: device update")
            device_id = resource.split("/")[1]
            responses.append((device_id, resource, response))

        # Base station its child device statuses. We split this apart here
        # and pass directly to the referenced devices.
        # Packet type #4
        elif resource == 'devices':
            self.debug("packet: base and child statuses")
            for device_id in response.get('devices', {}):
                self._arlo.debug(f"DEVICES={device_id}")
                props = response['devices'][device_id]
                responses.append((device_id, resource, props))

        # These are base station responses. Which can be about the base station
        # or devices on it... Check if property is list.
        # XXX these might be deprecated
        elif resource in self._resource_types:
            prop_or_props = response.get("properties", [])
            if isinstance(prop_or_props, list):
                for prop in prop_or_props:
                    device_id = prop.get("serialNumber", None)
                    if device_id is None:
                        device_id = response.get("from", None)
                    responses.append((device_id, resource, prop))
            else:
                device_id = response.get("from", None)
                responses.append((device_id, resource, response))

        # ArloBabyCam packets.
        elif resource.startswith("audioPlayback"):
            device_id = response.get("from")
            properties = response.get("properties")
            if resource == "audioPlayback/status":
                # Wrap the status event to match the 'audioPlayback' event
                properties = {"status": response.get("properties")}

            self._arlo.info(
                "audio playback response {} - {}".format(resource, response)
            )
            if device_id is not None and properties is not None:
                responses.append((device_id, resource, properties))

        # This a list ditch effort to funnel the answer the correct place...
        #  Check for device_id
        #  Check for unique_id
        #  Check for locationId
        # If none of those then is unhandled
        else:
            device_id = response.get("deviceId",
                                     response.get("uniqueId",
                                                  response.get("locationId", None)))
            if device_id is not None:
                responses.append((device_id, resource, response))
            else:
                self.debug(f"unhandled response {resource} - {response}")

        # Now find something waiting for this/these.
        for device_id, resource, response in responses:
            cbs = []
            self.debug("sending {} to {}".format(resource, device_id))
            with self._lock:
                if device_id and device_id in self._callbacks:
                    cbs.extend(self._callbacks[device_id])
                if "all" in self._callbacks:
                    cbs.extend(self._callbacks["all"])
            for cb in cbs:
                self._arlo.bg.run(cb, resource=resource, event=response)

    def _event_handle_response(self, response):

        # Debugging.
        if self._dump_file is not None:
            with open(self._dump_file, "a") as dump:
                time_stamp = now_strftime("%Y-%m-%d %H:%M:%S.%f")
                dump.write(
                    "{}: {}\n".format(
                        time_stamp, pprint.pformat(response, indent=2)
                    )
                )
        self.vdebug(
            "packet-in=\n{}".format(pprint.pformat(response, indent=2))
        )

        # Run the dispatcher to set internal state and run callbacks.
        self._event_dispatcher(response)

        # is there a notify/post waiting for this response? If so, signal to waiting entity.
        tid = response.get("transId", None)
        resource = response.get("resource", None)
        device_id = response.get("from", None)
        with self._lock:
            # Transaction ID
            # Simple. We have a transaction ID, look for that. These are
            # usually returned by notify requests.
            if tid and tid in self._requests:
                self._requests[tid] = response
                self._lock.notify_all()

            # Resource
            # These are usually returned after POST requests. We trap these
            # to make async calls sync.
            if resource:
                # Historical. We are looking for a straight matching resource.
                if resource in self._requests:
                    self.vdebug("{} found by text!".format(resource))
                    self._requests[resource] = response
                    self._lock.notify_all()

                else:
                    # Complex. We are looking for a resource and-or
                    # deviceid matching a regex.
                    if device_id:
                        resource = "{}:{}".format(resource, device_id)
                        self.vdebug("{} bounded device!".format(resource))
                    for request in self._requests:
                        if re.match(request, resource):
                            self.vdebug(
                                "{} found by regex {}!".format(resource, request)
                            )
                            self._requests[request] = response
                            self._lock.notify_all()

    def _event_stop_loop(self):
        self._stop_thread = True

    def _event_main(self):
        self.debug("re-logging in")

        while not self._stop_thread:

            # say we're starting
            if self._dump_file is not None:
                with open(self._dump_file, "a") as dump:
                    time_stamp = now_strftime("%Y-%m-%d %H:%M:%S.%f")
                    dump.write("{}: {}\n".format(time_stamp, "event_thread start"))

            # login again if not first iteration, this will also create a new session
            while not self._logged_in:
                with self._lock:
                    self._lock.wait(5)
                self.debug("re-logging in")
                self._logged_in = self._login()

            if self._use_mqtt:
                self._mqtt_main()
            else:
                self._sse_main()
            self.debug("exited the event loop")

            # clear down and signal out
            with self._lock:
                self._client_connected = False
                self._requests = {}
                self._lock.notify_all()

            # restart login...
            self._event_client = None
            self._logged_in = False

    def _mqtt_topics(self):
        topics = []
        for device in self._arlo.devices:
            for topic in device.get("allowedMqttTopics", []):
                topics.append((topic, 0))
        return topics

    def _mqtt_subscribe(self):
        # Make sure we are listening to library events and individual base
        # station events. This seems sufficient for now.
        self._event_client.subscribe([
            (f"u/{self._user_id}/in/userSession/connect", 0),
            (f"u/{self._user_id}/in/userSession/disconnect", 0),
            (f"u/{self._user_id}/in/library/add", 0),
            (f"u/{self._user_id}/in/library/update", 0),
            (f"u/{self._user_id}/in/library/remove", 0)
        ])

        topics = self._mqtt_topics()
        self.debug("topics=\n{}".format(pprint.pformat(topics)))
        self._event_client.subscribe(topics)

    def _mqtt_on_connect(self, _client, _userdata, _flags, rc):
        # Subscribing in on_connect() means that if we lose the connection and
        # reconnect then subscriptions will be renewed.
        self.debug(f"mqtt: connected={str(rc)}")
        self._mqtt_subscribe()
        with self._lock:
            self._event_connected = True
            self._lock.notify_all()

    def _mqtt_on_log(self, _client, _userdata, _level, msg):
        self.vdebug(f"mqtt: log={str(msg)}")

    def _mqtt_on_message(self, _client, _userdata, msg):
        self.debug(f"mqtt: topic={msg.topic}")
        try:
            response = json.loads(msg.payload.decode("utf-8"))

            # deal with mqtt specific pieces
            if response.get("action", "") == "logout":
                # Logged out? MQTT will log back in until stopped.
                self._arlo.warning("logged out? did you log in from elsewhere?")
                return

            # pass on to general handler
            self._event_handle_response(response)

        except json.decoder.JSONDecodeError as e:
            self.debug("reopening: json error " + str(e))

    def _mqtt_main(self):

        try:
            self.debug("(re)starting mqtt event loop")
            headers = {
                "Host": MQTT_HOST,
                "Origin": ORIGIN_HOST,
            }

            # Build a new client_id per login. The last 10 numbers seem to need to be random.
            self._event_client_id = f"user_{self._user_id}_" + "".join(
                str(random.randint(0, 9)) for _ in range(10)
            )
            self.debug(f"mqtt: client_id={self._event_client_id}")

            # Create and set up the MQTT client.
            self._event_client = mqtt.Client(
                client_id=self._event_client_id, transport=self._arlo.cfg.mqtt_transport
            )
            self._event_client.on_log = self._mqtt_on_log
            self._event_client.on_connect = self._mqtt_on_connect
            self._event_client.on_message = self._mqtt_on_message
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = self._arlo.cfg.mqtt_hostname_check
            self._event_client.tls_set_context(ssl_context)
            self._event_client.username_pw_set(f"{self._user_id}", self._token)
            self._event_client.ws_set_options(path=MQTT_PATH, headers=headers)
            self.debug(f"mqtt: host={self._arlo.cfg.mqtt_host}, "
                       f"check={self._arlo.cfg.mqtt_hostname_check}, "
                       f"transport={self._arlo.cfg.mqtt_transport}")

            # Connect.
            self._event_client.connect(self._arlo.cfg.mqtt_host, port=self._arlo.cfg.mqtt_port, keepalive=60)
            self._event_client.loop_forever()

        except Exception as e:
            # self._arlo.warning('general exception ' + str(e))
            self._arlo.error(
                "mqtt-error={}\n{}".format(
                    type(e).__name__, traceback.format_exc()
                )
            )

    def _sse_reconnected(self):
        self.debug("fetching device list after ev-reconnect")
        self.devices()

    def _sse_reconnect(self):
        self.debug("trying to reconnect")
        if self._event_client is not None:
            self._event_client.stop()

    def _sse_main(self):

        # get stream, restart after requested seconds of inactivity or forced close
        try:
            if self._arlo.cfg.stream_timeout == 0:
                self.debug("starting stream with no timeout")
                self._event_client = SSEClient(
                    self._arlo,
                    self._arlo.cfg.host + SUBSCRIBE_PATH,
                    headers=self._headers(),
                    reconnect_cb=self._sse_reconnected,
                )
            else:
                self.debug(
                    "starting stream with {} timeout".format(
                        self._arlo.cfg.stream_timeout
                    )
                )
                self._event_client = SSEClient(
                    self._arlo,
                    self._arlo.cfg.host + SUBSCRIBE_PATH,
                    headers=self._headers(),
                    reconnect_cb=self._sse_reconnected,
                    timeout=self._arlo.cfg.stream_timeout,
                )

            for event in self._event_client:

                # stopped?
                if event is None:
                    self.debug("reopening: no event")
                    break

                # dig out response
                try:
                    response = json.loads(event.data)
                except json.decoder.JSONDecodeError as e:
                    self.debug("reopening: json error " + str(e))
                    break

                # deal with SSE specific pieces
                # logged out? signal exited
                if response.get("action", "") == "logout":
                    self._arlo.warning("logged out? did you log in from elsewhere?")
                    break

                # connected - yay!
                if response.get("status", "") == "connected":
                    with self._lock:
                        self._event_connected = True
                        self._lock.notify_all()
                    continue

                # pass on to general handler
                self._event_handle_response(response)

        except requests.exceptions.ConnectionError:
            self._arlo.warning("event loop timeout")
        except requests.exceptions.HTTPError:
            self._arlo.warning("event loop closed by server")
        except AttributeError as e:
            self._arlo.warning("forced close " + str(e))
        except Exception as e:
            # self._arlo.warning('general exception ' + str(e))
            self._arlo.error(
                "sse-error={}\n{}".format(
                    type(e).__name__, traceback.format_exc()
                )
            )

    def _select_backend(self):
        # determine backend to use
        if self._arlo.cfg.event_backend == 'auto':
            if len(self._mqtt_topics()) == 0:
                self.debug("auto chose SSE backend")
                self._use_mqtt = False
            else:
                self.debug("auto chose MQTT backend")
                self._use_mqtt = True
        elif self._arlo.cfg.event_backend == 'mqtt':
            self.debug("user chose MQTT backend")
            self._use_mqtt = True
        else:
            self.debug("user chose SSE backend")
            self._use_mqtt = False

    def start_monitoring(self):
        self._select_backend()
        self._event_client = None
        self._event_connected = False
        self._event_thread = threading.Thread(
            name="ArloEventStream", target=self._event_main, args=()
        )
        self._event_thread.daemon = True

        with self._lock:
            self._event_thread.start()
            count = 0
            while not self._event_connected and count < 30:
                self.debug("waiting for stream up")
                self._lock.wait(1)
                count += 1

        # start logout daemon for sse clients
        if not self._use_mqtt:
            if self._arlo.cfg.reconnect_every != 0:
                self.debug("automatically reconnecting")
                self._arlo.bg.run_every(self._sse_reconnect, self._arlo.cfg.reconnect_every)

        self.debug("stream up")
        return True
    
    def _get_tfa(self):
        """Return the 2FA type we're using."""
        tfa_type = self._arlo.cfg.tfa_source
        if tfa_type == TFA_CONSOLE_SOURCE:
            return Arlo2FAConsole(self._arlo)
        elif tfa_type == TFA_IMAP_SOURCE:
            return Arlo2FAImap(self._arlo)
        elif tfa_type == TFA_REST_API_SOURCE:
            return Arlo2FARestAPI(self._arlo)
        else:
            return tfa_type

    def _update_auth_info(self, body):
        if "accessToken" in body:
            body = body["accessToken"]
        self._token = body["token"]
        self._token64 = to_b64(self._token)
        self._user_id = body["userId"]
        self._web_id = self._user_id + "_web"
        self._sub_id = "subscriptions/" + self._web_id
        self._expires_in = body["expiresIn"]
        if "browserAuthCode" in body:
            self.debug("browser auth code: {}".format(body["browserAuthCode"]))
            self._browser_auth_code = body["browserAuthCode"]

    def _auth_headers(self):
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "en-GB,en;q=0.9,en-US;q=0.8",
            "Cache-Control": "no-cache",
            "Content-Type": "application/json",
            # "Dnt": "1",
            "Origin": ORIGIN_HOST,
            "Pragma": "no-cache",
            "Priority": "u=1, i",
            "Referer": REFERER_HOST,
            # "Sec-Ch-Ua": '"Not.A/Brand";v="8", "Chromium";v="114", "Google Chrome";v="114"',
            # "Sec-Ch-Ua-Mobile": "?0",
            # "Sec-Ch-Ua-Platform": "Linux",
            # "Sec-Fetch-Dest": "empty",
            # "Sec-Fetch-Mode": "cors",
            # "Sec-Fetch-Site": "same-site",
            "User-Agent": self._user_agent,
            "X-Service-Version": "3",
            "X-User-Device-Automation-Name": "QlJPV1NFUg==",
            "X-User-Device-Id": self._user_device_id,
            "X-User-Device-Type": "BROWSER",
        }

        # Add Source if asked for.
        if self._arlo.cfg.send_source:
            headers.update({
                "Source": "arloCamWeb",
            })

        return headers

    def _headers(self):
        return {
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "en-GB,en;q=0.9,en-US;q=0.8",
            "Auth-Version": "2",
            "Authorization": self._token,
            "Cache-Control": "no-cache",
            "Content-Type": "application/json; charset=utf-8;",
            # "Dnt": "1",
            "Origin": ORIGIN_HOST,
            "Pragma": "no-cache",
            "Priority": "u=1, i",
            "Referer": REFERER_HOST,
            "SchemaVersion": "1",
            # "Sec-Ch-Ua": '"Not.A/Brand";v="8", "Chromium";v="114", "Google Chrome";v="114"',
            # "Sec-Ch-Ua-Mobile": "?0",
            # "Sec-Ch-Ua-Platform": "Linux",
            # "Sec-Fetch-Dest": "empty",
            # "Sec-Fetch-Mode": "cors",
            # "Sec-Fetch-Site": "same-site",
            "User-Agent": self._user_agent,
        }

    def _auth(self) -> AuthResult:
        headers = self._auth_headers()

        # Handle 1015 error
        attempt = 0
        code = 0
        body = None
        while attempt < 3:
            attempt += 1
            self.debug("login attempt #{}".format(attempt))
            self._options = self.auth_options(AUTH_PATH, headers)

            code, body = self.auth_post(
                AUTH_PATH,
                {
                    "email": self._arlo.cfg.username,
                    "password": to_b64(self._arlo.cfg.password),
                    "language": "en",
                    "EnvSource": "prod",
                },
                headers,
            )
            if code == 200 or code == 401:
                break
            time.sleep(3)

        if body is None:
            self._arlo.error(f"login failed: {code} - possible cloudflare issue")
            return AuthResult.CAN_RETRY
        if code != 200:
            self._arlo.error(f"login failed: {code} - {body}")
            return AuthResult.FAILED

        # save new login information
        self._update_auth_info(body)

        # Looks like we need 2FA. So, request a code be sent to our email address.
        if not body["authCompleted"]:
            self.debug("need 2FA...")

            # update headers and create 2fa instance
            headers["Authorization"] = self._token64
            tfa = self._get_tfa()

            # get available 2fa choices,
            self.debug("getting tfa choices")

            self._options = self.auth_options(AUTH_GET_FACTORID, headers)

            # look for code source choice
            self.debug(f"looking for {self._arlo.cfg.tfa_type}/{self._arlo.cfg.tfa_nickname}")
            factors_of_type = []
            factor_id = None

            payload = {
                "factorType": "BROWSER",
                "factorData": "",
                "userId": self._user_id
            }

            code, body = self.auth_post(
                AUTH_GET_FACTORID, payload, headers, cookies=self._cookies
            )

            if code == 200:
                self._needs_pairing = False
                factor_id = body["factorId"]
            else:
                self._needs_pairing = True
                factors = self.auth_get(
                    AUTH_GET_FACTORS + "?data = {}".format(int(time.time())), {}, headers
                )
                if factors is None:
                    self._arlo.error("login failed: 2fa: no secondary choices available")
                    return AuthResult.FAILED

                for factor in factors["items"]:
                    if factor["factorType"].lower() == self._arlo.cfg.tfa_type:
                        factors_of_type.append(factor)

                if len(factors_of_type) > 0:
                    # Try to match the factorNickname with the tfa_nickname
                    for factor in factors_of_type:
                        if self._arlo.cfg.tfa_nickname == factor["factorNickname"]:
                            factor_id = factor["factorId"]
                            break
                    # Otherwise fallback to using the first option
                    else:
                        factor_id = factors_of_type[0]["factorId"]

            if factor_id is None:
                self._arlo.error("login failed: 2fa: no secondary choices available")
                return AuthResult.FAILED

            if code == 200:
                payload = {
                    "factorId": factor_id,
                    "factorType": "BROWSER",
                    "userId": self._user_id
                }
                self._options = self.auth_options(AUTH_START_PATH, headers)
                code, body = self.auth_post(AUTH_START_PATH, payload, headers)
                if code != 200:
                    self._arlo.error(f"login failed: quick start failed: {code} - {body}")
                    return AuthResult.FAILED

            elif tfa != TFA_PUSH_SOURCE:
                # snapshot 2fa before sending in request
                if not tfa.start():
                    self._arlo.error("login failed: 2fa: startup failed")
                    return AuthResult.FAILED

                # start authentication with email
                self.debug(
                    "starting auth with {}".format(self._arlo.cfg.tfa_type)
                )
                payload = {
                    "factorId": factor_id,
                    "factorType": "BROWSER",
                    "userId": self._user_id
                }
                self._options = self.auth_options(AUTH_START_PATH, headers)
                code, body = self.auth_post(AUTH_START_PATH, payload, headers)
                if code != 200:
                    self._arlo.error(f"login failed: start failed: {code} - {body}")
                    return AuthResult.CAN_RETRY
                factor_auth_code = body["factorAuthCode"]

                # get code from TFA source
                code = tfa.get()
                if code is None:
                    self._arlo.error(f"login failed: 2fa: code retrieval failed")
                    return AuthResult.CAN_RETRY

                # tidy 2fa
                tfa.stop()

                # finish authentication
                self.debug("finishing auth")
                code, body = self.auth_post(
                    AUTH_FINISH_PATH, {
                        "factorAuthCode": factor_auth_code,
                        "otp": code,
                        "isBrowserTrusted": True
                    },
                    headers,
                )
                if code != 200:
                    self._arlo.error(f"login failed: finish failed: {code} - {body}")
                    return AuthResult.FAILED
            else:
                # start authentication
                self.debug(
                    "starting auth with {}".format(self._arlo.cfg.tfa_type)
                )
                payload = {
                    "factorId": factor_id,
                    "factorType": "",
                    "userId": self._user_id
                }
                code, body = self.auth_post(AUTH_START_PATH, payload, headers)
                if code != 200:
                    self._arlo.error(f"login failed: start failed: {code} - {body}")
                    return AuthResult.FAILED
                factor_auth_code = body["factorAuthCode"]
                tries = 1
                while True:
                    # finish authentication
                    self.debug("finishing auth")
                    code, body = self.auth_post(
                        AUTH_FINISH_PATH, {
                            "factorAuthCode": factor_auth_code,
                            "isBrowserTrusted": True
                        },
                        headers,
                    )
                    if code != 200:
                        self._arlo.warning("2fa finishAuth - tries {}".format(tries))
                        if tries < self._arlo.cfg.tfa_retries:
                            time.sleep(self._arlo.cfg.tfa_delay)
                            tries += 1
                        else:
                            self._arlo.error(f"login failed: finish failed: {code} - {body}")
                            return AuthResult.FAILED
                    else:
                        break

            # save new login information
            self._update_auth_info(body)

        return AuthResult.SUCCESS

    def _validate(self):
        headers = self._auth_headers()
        headers["Authorization"] = self._token64

        # Validate it!
        validated = self.auth_get(
            AUTH_VALIDATE_PATH + "?data = {}".format(int(time.time())), {}, headers
        )
        if validated is None:
            self._arlo.error("token validation failed")
            return False
        return True

    def _pair_auth_code(self):
        headers = self._auth_headers()
        headers["Authorization"] = self._token64

        if not self._needs_pairing:
            self._arlo.debug("no pairing required")
            self._save_cookies(self._cookies)
            return True
        if self._browser_auth_code is None:
            self._arlo.debug("pairing postponed")
            return True

        # self._cookies = self._load_cookies()
        payload = {
            "factorAuthCode": self._browser_auth_code,
            "factorData": "",
            "factorType": "BROWSER"
        }
        code, body = self.auth_post(AUTH_START_PAIRING, payload, headers, cookies=self._cookies)
        self._save_cookies(self._cookies)

        if code != 200:
            self._arlo.error(f"pairing: failed: {code} - {body}")
            return False

        self._arlo.debug("pairing succeeded")
        return True

    def _v2_session(self):
        v2_session = self.get(SESSION_PATH)
        if v2_session is None:
            self._arlo.error("session start failed")
            return False
        self._multi_location = v2_session.get('supportsMultiLocation', False)
        self._arlo.debug(f"multilocation is {self._multi_location}")

        # If Arlo provides an MQTT URL key use it to set the backend.
        if MQTT_URL_KEY in v2_session:
            self._arlo.cfg.update_mqtt_from_url(v2_session[MQTT_URL_KEY])
            self._arlo.debug(f"back={self._arlo.cfg.event_backend};url={self._arlo.cfg.mqtt_host}:{self._arlo.cfg.mqtt_port}")
        return True

    def _login(self):

        # pickup user configured user agent
        self._user_agent = self.user_agent(self._arlo.cfg.user_agent)

        # we always login but and let the backend determine if we need to
        # use 2fa
        success = AuthResult.FAILED
        for curve in self._arlo.cfg.ecdh_curves:
            self.debug(f"CloudFlare curve set to: {curve}")
            self._session = cloudscraper.create_scraper(
                # browser={
                #     'browser': 'chrome',
                #     'platform': 'darwin',
                #     'desktop': True,
                #     'mobile': False,
                # },
                disableCloudflareV1=True,
                ecdhCurve=curve,
                debug=False,
            )
            self._session.cookies = self._cookies

            # Try to authenticate. We retry if it was a cloud flare
            # error or we failed to get the 2FA code.
            success = self._auth()
            if success == AuthResult.FAILED:
                return False
            if success == AuthResult.SUCCESS and self._validate() and self._pair_auth_code():
                break
            success = AuthResult.FAILED
            self.debug("login failed, trying another ecdh_curve")

        if success != AuthResult.SUCCESS:
            return False

        # save session in case we updated it
        self._save_session()

        # update sessions headers
        headers = self._headers()
        self._session.headers.update(headers)

        # Grab a session. Needed for new session and used to check existing
        # session. (May not really be needed for existing but will fail faster.)
        if not self._v2_session():
            return False
        return True

    def _notify(self, base, body, trans_id=None):
        if trans_id is None:
            trans_id = self.gen_trans_id()

        body["to"] = base.device_id
        if "from" not in body:
            body["from"] = self._web_id
        body["transId"] = trans_id

        response = self.post(
            NOTIFY_PATH + base.device_id, body, headers={"xcloudId": base.xcloud_id}
        )

        if response is None:
            return None
        else:
            return trans_id

    def _start_transaction(self, tid=None):
        if tid is None:
            tid = self.gen_trans_id()
        self.vdebug("starting transaction-->{}".format(tid))
        with self._lock:
            self._requests[tid] = None
        return tid

    def _wait_for_transaction(self, tid, timeout):
        if timeout is None:
            timeout = self._arlo.cfg.request_timeout
        mnow = time.monotonic()
        mend = mnow + timeout

        self.vdebug("finishing transaction-->{}".format(tid))
        with self._lock:
            try:
                while mnow < mend and self._requests[tid] is None:
                    self._lock.wait(mend - mnow)
                    mnow = time.monotonic()
                response = self._requests.pop(tid)
            except KeyError as _e:
                self.debug("got a key error")
                response = None
        self.vdebug("finished transaction-->{}".format(tid))
        return response

    @property
    def is_connected(self):
        return self._logged_in

    def logout(self):
        self.debug("trying to logout")
        self._event_stop_loop()
        if self._event_client is not None:
            if self._use_mqtt:
                self._event_client.disconnect()
            else:
                self._event_client.stop()
        self.put(LOGOUT_PATH)

    def notify(self, base, body, timeout=None, wait_for=None):
        """Send in a notification.

        Notifications are Arlo's way of getting stuff done - turn on a light, change base station mode,
        start recording. Pyaarlo will post a notification and Arlo will post a reply on the event
        stream indicating if it worked or not or of a state change.

        How Pyaarlo treats notifications depends on the mode it's being run in. For asynchronous mode - the
        default - it sends the notification and returns immediately. For synchronous mode it sends the
        notification and waits for the event related to the notification to come back. To use the default
        settings leave `wait_for` as `None`, to force asynchronous set `wait_for` to `nothing` and to force
        synchronous set `wait_for` to `event`.

        There is a third way to send a notification where the code waits for the initial response to come back
        but that must be specified by setting `wait_for` to `response`.

        :param base: base station to use
        :param body: notification message
        :param timeout: how long to wait for response before failing, only applied if `wait_for` is `event`.
        :param wait_for: what to wait for, either `None`, `event`, `response` or `nothing`.
        :return: either a response packet or an event packet
        """
        if wait_for is None:
            wait_for = "event" if self._arlo.cfg.synchronous_mode else "nothing"

        if wait_for == "event":
            self.vdebug("notify+event running")
            tid = self._start_transaction()
            self._notify(base, body=body, trans_id=tid)
            return self._wait_for_transaction(tid, timeout)
            # return self._notify_and_get_event(base, body, timeout=timeout)
        elif wait_for == "response":
            self.vdebug("notify+response running")
            return self._notify(base, body=body)
        else:
            self.vdebug("notify+ sent")
            self._arlo.bg.run(self._notify, base=base, body=body)

    def get(
        self,
        path,
        params=None,
        headers=None,
        stream=False,
        raw=False,
        timeout=None,
        host=None,
        wait_for="response",
        cookies=None,
    ):
        if wait_for == "response":
            self.vdebug("get+response running")
            return self._request(
                path, "GET", params, headers, stream, raw, timeout, host, cookies
            )
        else:
            self.vdebug("get sent")
            self._arlo.bg.run(
                self._request, path, "GET", params, headers, stream, raw, timeout, host
            )

    def put(
        self,
        path,
        params=None,
        headers=None,
        raw=False,
        timeout=None,
        wait_for="response",
        cookies=None,
    ):
        if wait_for == "response":
            self.vdebug("put+response running")
            return self._request(path, "PUT", params, headers, False, raw, timeout, cookies)
        else:
            self.vdebug("put sent")
            self._arlo.bg.run(
                self._request, path, "PUT", params, headers, False, raw, timeout
            )

    def post(
        self,
        path,
        params=None,
        headers=None,
        raw=False,
        timeout=None,
        tid=None,
        wait_for="response"
    ):
        """Post a request to the Arlo servers.

        Posts are used to retrieve data from the Arlo servers. Mostly. They are also used to change
        base station modes.

        The default mode of operation is to wait for a response from the http request. The `wait_for`
        variable can change the operation. Setting it to `response` waits for a http response.
        Setting it to `resource` waits for the resource in the `params` parameter to appear in the event
        stream. Setting it to `nothing` causing the post to run in the background. Setting it to `None`
        uses `resource` in synchronous mode and `response` in asynchronous mode.
        """
        if wait_for is None:
            wait_for = "resource" if self._arlo.cfg.synchronous_mode else "response"

        if wait_for == "resource":
            self.vdebug("notify+resource running")
            if tid is None:
                tid = list(params.keys())[0]
            tid = self._start_transaction(tid)
            self._request(path, "POST", params, headers, False, raw, timeout)
            return self._wait_for_transaction(tid, timeout)
        if wait_for == "response":
            self.vdebug("post+response running")
            return self._request(path, "POST", params, headers, False, raw, timeout)
        else:
            self.vdebug("post sent")
            self._arlo.bg.run(
                self._request, path, "POST", params, headers, False, raw, timeout
            )

    def auth_post(self, path, params=None, headers=None, raw=False, timeout=None, cookies=None):
        return self._request_tuple(
            path, "POST", params, headers, False, raw, timeout, self._arlo.cfg.auth_host, authpost=True, cookies=cookies
        )

    def auth_get(
        self, path, params=None, headers=None, stream=False, raw=False, timeout=None, cookies=None
    ):
        return self._request(
            path, "GET", params, headers, stream, raw, timeout, self._arlo.cfg.auth_host, authpost=True, cookies=cookies
        )

    def auth_options(
        self, path, headers=None, timeout=None
     ):
        return self._request(
            path, "OPTIONS", None, headers, False, False, timeout, self._arlo.cfg.auth_host, authpost=True
        )

    @property
    def session(self):
        return self._session

    @property
    def sub_id(self):
        return self._sub_id

    @property
    def user_id(self):
        return self._user_id

    @property
    def multi_location(self):
        return self._multi_location

    def add_listener(self, device, callback):
        with self._lock:
            if device.device_id not in self._callbacks:
                self._callbacks[device.device_id] = []
            self._callbacks[device.device_id].append(callback)
            if device.unique_id not in self._callbacks:
                self._callbacks[device.unique_id] = []
            self._callbacks[device.unique_id].append(callback)

    def add_any_listener(self, callback):
        with self._lock:
            if "all" not in self._callbacks:
                self._callbacks["all"] = []
            self._callbacks["all"].append(callback)

    def del_listener(self, device, callback):
        pass

    def devices(self):
        return self.get(DEVICES_PATH + "?t={}".format(time_to_arlotime()))

    def user_agent(self, agent):
        """Map `agent` to a real user agent.

        User provides a default user agent they want for most interactions but it can be overridden
        for stream operations.

        `!real-string` will use the provided string as-is, used when passing user agent
        from a browser.

        `random` will provide a different user agent for each log in attempt.
        """
        if agent.startswith("!"):
            self.debug(f"using user supplied user_agent {agent[:70]}")
            return agent[1:]
        agent = agent.lower()
        self.debug(f"looking for user_agent {agent}")
        if agent == "random":
            return self.user_agent(random.choice(list(USER_AGENTS.keys())))
        return USER_AGENTS.get(agent, USER_AGENTS["linux"])

    def ev_inject(self, response):
        self._event_dispatcher(response)

    def debug(self, msg):
        self._arlo.debug(f"backend: {msg}")

    def vdebug(self, msg):
        self._arlo.vdebug(f"backend: {msg}")
