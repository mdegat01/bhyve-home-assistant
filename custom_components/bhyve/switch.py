"""Support for Orbit BHyve switch (toggle zone)."""
import datetime
import logging

from datetime import timedelta

try:
    from homeassistant.components.switch import DEVICE_CLASS_SWITCH, SwitchEntity
except ImportError:
    from homeassistant.components.switch import (
        DEVICE_CLASS_SWITCH,
        SwitchDevice as SwitchEntity,
    )

from homeassistant.core import callback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_send,
    async_dispatcher_connect,
)
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt


from . import BHyveWebsocketEntity, BHyveDeviceEntity
from .const import (
    DEVICE_SPRINKLER,
    DOMAIN,
    EVENT_CHANGE_MODE,
    EVENT_DEVICE_IDLE,
    EVENT_PROGRAM_CHANGED,
    EVENT_RAIN_DELAY,
    EVENT_SET_MANUAL_PRESET_TIME,
    EVENT_WATERING_COMPLETE,
    EVENT_WATERING_IN_PROGRESS,
    SIGNAL_UPDATE_PROGRAM,
)
from .pybhyve.errors import BHyveError
from .util import orbit_time_to_local_time

_LOGGER = logging.getLogger(__name__)

DEFAULT_MANUAL_RUNTIME = timedelta(minutes=10)

PROGRAM_SMART_WATERING = "e"
PROGRAM_MANUAL = "manual"

ATTR_MANUAL_RUNTIME = "manual_preset_runtime"
ATTR_SMART_WATERING_ENABLED = "smart_watering_enabled"
ATTR_SPRINKLER_TYPE = "sprinkler_type"
ATTR_IMAGE_URL = "image_url"
ATTR_STARTED_WATERING_AT = "started_watering_station_at"
ATTR_SMART_WATERING_PLAN = "watering_program"

# Rain Delay Attributes
ATTR_CAUSE = "cause"
ATTR_DELAY = "delay"
ATTR_WEATHER_TYPE = "weather_type"
ATTR_STARTED_AT = "started_at"

ATTR_PROGRAM = "program_{}"


async def async_setup_platform(hass, config, async_add_entities, _discovery_info=None):
    """Set up BHyve binary sensors based on a config entry."""
    bhyve = hass.data[DOMAIN]

    switches = []
    devices = await bhyve.devices
    programs = await bhyve.timer_programs
    for device in devices:
        if device.get("type") == DEVICE_SPRINKLER:
            for zone in device.get("zones"):
                switches.append(
                    BHyveZoneSwitch(hass, bhyve, device, zone, programs, "water-pump")
                )
                switches.append(
                    BHyveRainDelaySwitch(hass, bhyve, device, "weather-pouring")
                )

    for program in programs:
        _LOGGER.info("Creating switch: Program %s", program.get("name"))
        switches.append(BHyveProgramSwitch(hass, bhyve, program, "water-pump"))

    async_add_entities(switches, True)


class BHyveProgramSwitch(BHyveWebsocketEntity, SwitchEntity):
    """Define a BHyve program switch."""

    def __init__(self, hass, bhyve, program, icon):
        """Initialize the switch."""
        name = "{} Program".format(program.get("name"))

        super().__init__(hass, bhyve, name, icon, DEVICE_CLASS_SWITCH)

        self._program = program
        self._device_id = program.get("device_id")
        self._program_id = program.get("id")
        self._available = True

    @property
    def device_state_attributes(self):
        """Return the device state attributes."""

        attrs = {
            "device_id": self._device_id,
            "is_smart_program": self._program.get("is_smart_program", False),
            "frequency": self._program.get("frequency"),
            "start_times": self._program.get("start_times"),
            "budget": self._program.get("budget"),
            "program": self._program.get("program"),
            "run_times": self._program.get("run_times"),
        }

        return attrs

    @property
    def is_on(self):
        """Return the status of the sensor."""
        return self._program.get("enabled") is True

    @property
    def unique_id(self):
        return "bhyve:program:{}".format(self._program.get("id"))

    async def _set_state(self, is_on):
        self._program.update({"enabled": is_on})
        await self._bhyve.update_program(self._program_id, self._program)

    async def async_turn_on(self, **kwargs):
        """Turn the switch on."""
        await self._set_state(True)

    async def async_turn_off(self, **kwargs):
        """Turn the switch off."""
        await self._set_state(False)

    async def async_added_to_hass(self):
        """Register callbacks."""

        @callback
        def update(device_id, data):
            """Update the state."""
            _LOGGER.info(
                "Program update: {} - {} - {}".format(
                    self.name, self._program_id, str(data)[:160]
                )
            )
            event = data.get("event")
            if event == "program_changed":
                self._ws_unprocessed_events.append(data)
                self.async_schedule_update_ha_state(True)

        self._async_unsub_dispatcher_connect = async_dispatcher_connect(
            self.hass, SIGNAL_UPDATE_PROGRAM.format(self._program_id), update
        )

    async def async_will_remove_from_hass(self):
        """Disconnect dispatcher listener when removed."""
        if self._async_unsub_dispatcher_connect:
            self._async_unsub_dispatcher_connect()

    def _on_ws_data(self, data):
        """
            {'event': 'program_changed' }
        """
        _LOGGER.info("Received program data update {}".format(data))

        event = data.get("event")
        if event is None:
            _LOGGER.warning("No event on ws data {}".format(data))
            return
        elif event == "program_changed":
            program = data.get("program")
            if program is not None:
                self._program = program

    def _should_handle_event(self, event_name):
        return event_name in ["program_changed"]


class BHyveZoneSwitch(BHyveDeviceEntity, SwitchEntity):
    """Define a BHyve zone switch."""

    def __init__(self, hass, bhyve, device, zone, programs, icon):
        """Initialize the switch."""
        self._zone = zone
        self._zone_id = zone.get("station")
        self._entity_picture = zone.get("image_url")
        self._zone_name = zone.get("name")
        self._manual_preset_runtime = device.get(
            "manual_preset_runtime_sec", DEFAULT_MANUAL_RUNTIME.seconds
        )

        # Filter out any programs which are not for this device
        self._initial_programs = list(
            filter(
                lambda program: (program.get("device_id") == device.get("device_id")),
                programs or []
            )
        )

        self._initial_programs = programs

        name = f"{self._zone_name} zone"
        _LOGGER.info("Creating switch: %s", name)

        super().__init__(hass, bhyve, device, name, icon, DEVICE_CLASS_SWITCH)

    def _setup(self, device):
        self._is_on = False
        self._attrs = {
            "device_name": self._device_name,
            "device_id": self._device_id,
        }
        self._available = device.get("is_connected", False)

        status = device.get("status", {})
        watering_status = status.get("watering_status")

        _LOGGER.info("{} watering_status: {}".format(self.name, watering_status))

        zones = device.get("zones", [])

        zone = None
        for z in zones:
            if z.get("station") == self._zone_id:
                zone = z
                break

        if zone is not None:
            is_watering = (
                watering_status is not None
                and watering_status.get("current_station") == self._zone_id
            )
            self._is_on = is_watering
            self._attrs[ATTR_MANUAL_RUNTIME] = self._manual_preset_runtime

            smart_watering_enabled = zone.get("smart_watering_enabled")
            if smart_watering_enabled is not None:
                self._attrs[ATTR_SMART_WATERING_ENABLED] = smart_watering_enabled

            sprinkler_type = zone.get("sprinkler_type")
            if sprinkler_type is not None:
                self._attrs[ATTR_SPRINKLER_TYPE] = sprinkler_type

            image_url = zone.get("image_url")
            if image_url is not None:
                self._attrs[ATTR_IMAGE_URL] = image_url

            if is_watering:
                started_watering_at = watering_status.get("started_watering_station_at")
                self._set_watering_started(started_watering_at)

        if self._initial_programs is not None:
            programs = self._initial_programs
            for program in programs:
                self._set_watering_program(program)
            self._initial_programs = None

    def _set_watering_started(self, timestamp):
        if timestamp is not None:
            self._attrs[ATTR_STARTED_WATERING_AT] = orbit_time_to_local_time(timestamp)
        else:
            self._attrs[ATTR_STARTED_WATERING_AT] = None

    def _set_watering_program(self, program):
        if program is None:
            return

        program_name = program.get("name", "Unknown")
        program_id = program.get("program")
        program_enabled = program.get("enabled", False)
        program_attr = ATTR_PROGRAM.format(program_id)

        # Filter out any run times which are not for this switch
        active_program_run_times = list(
            filter(
                lambda x: (x.get("station") == self._zone_id),
                program.get("run_times", []),
            )
        )

        is_smart_program = program.get("is_smart_program", False)

        self._attrs[program_attr] = {
            "enabled": program_enabled,
            "name": program_name,
            "is_smart_program": is_smart_program,
        }

        if not program_enabled or not active_program_run_times:
            _LOGGER.info(
                "{} Zone: Watering program {} ({}) is not enabled, skipping".format(
                    self._zone_name, program_name, program_id
                )
            )
            if is_smart_program == True:
                self._attrs[ATTR_SMART_WATERING_PLAN] = None

            return

        """
            "name": "Backyard",
            "frequency": { "type": "days", "days": [1, 4] },
            "start_times": ["07:30"],
            "budget": 100,
            "program": "a",
            "run_times": [{ "run_time": 20, "station": 1 }],
        """

        if is_smart_program == True:
            upcoming_run_times = []
            for plan in program.get("watering_plan", []):
                run_times = plan.get("run_times")
                if run_times:
                    zone_times = list(
                        filter(lambda x: (x.get("station") == self._zone_id), run_times)
                    )
                    if zone_times:
                        plan_date = orbit_time_to_local_time(plan.get("date"))
                        for time in plan.get("start_times", []):
                            t = dt.parse_time(time)
                            upcoming_run_times.append(
                                plan_date + timedelta(hours=t.hour, minutes=t.minute)
                            )
            self._attrs[ATTR_SMART_WATERING_PLAN] = upcoming_run_times
        else:
            self._attrs[program_attr].update(
                {
                    "start_times": program.get("start_times", []),
                    "frequency": program.get("frequency", []),
                    "run_times": active_program_run_times,
                }
            )

    def _on_ws_data(self, data):
        """
            {'event': 'change_mode', 'mode': 'auto', 'device_id': 'id', 'timestamp': '2020-01-09T20:30:00.000Z'}
            {'event': 'watering_in_progress_notification', 'program': 'e', 'current_station': 1, 'run_time': 14, 'started_watering_station_at': '2020-01-09T20:29:59.000Z', 'rain_sensor_hold': False, 'device_id': 'id', 'timestamp': '2020-01-09T20:29:59.000Z'}
            {'event': 'device_idle', 'device_id': 'id', 'timestamp': '2020-01-10T12:32:06.000Z'}
            {'event': 'set_manual_preset_runtime', 'device_id': 'id', 'seconds': 480, 'timestamp': '2020-01-18T17:00:35.000Z'}
            {'event': 'program_changed' }
        """
        event = data.get("event")
        if event is None:
            _LOGGER.warning("No event on ws data {}".format(data))
            return
        elif event == EVENT_DEVICE_IDLE or event == EVENT_WATERING_COMPLETE:
            self._is_on = False
            self._set_watering_started(None)
        elif event == EVENT_WATERING_IN_PROGRESS:
            zone = data.get("current_station")
            if zone == self._zone_id:
                self._is_on = True
                started_watering_at = data.get("started_watering_station_at")
                self._set_watering_started(started_watering_at)
        elif event == EVENT_CHANGE_MODE:
            program = data.get("program")
            self._is_on = program == PROGRAM_SMART_WATERING or program == PROGRAM_MANUAL
        elif event == EVENT_SET_MANUAL_PRESET_TIME:
            self._manual_preset_runtime = data.get("seconds")
            self._attrs[ATTR_MANUAL_RUNTIME] = self._manual_preset_runtime
        elif event == EVENT_PROGRAM_CHANGED:
            watering_program = data.get("program")
            lifecycle_phase = data.get("lifecycle_phase")
            if lifecycle_phase != "destroy":
                self._set_watering_program(watering_program)
            else:
                self._attrs[ATTR_SMART_WATERING_PLAN] = None

    async def _send_station_message(self, station_payload):
        try:
            now = datetime.datetime.now()
            iso_time = now.strftime("%Y-%m-%dT%H:%M:%SZ")

            payload = {
                "event": EVENT_CHANGE_MODE,
                "mode": "manual",
                "device_id": self._device_id,
                "timestamp": iso_time,
                "stations": station_payload,
            }
            _LOGGER.info("Starting watering")
            await self._bhyve.send_message(payload)

        except BHyveError as err:
            _LOGGER.warning("Failed to send to BHyve websocket message %s", err)
            raise (err)

    @property
    def entity_picture(self):
        return self._entity_picture

    @property
    def unique_id(self):
        """Return a unique, unchanging string that represents this sensor."""
        return f"{self._mac_address}:{self._device_type}:zone:{self._zone_id}"

    @property
    def is_on(self):
        """Return the status of the sensor."""
        return self._is_on

    async def async_turn_on(self, **kwargs):
        """Turn the switch on."""
        station_payload = [
            {"station": self._zone_id, "run_time": self._manual_preset_runtime / 60}
        ]
        self._is_on = True
        await self._send_station_message(station_payload)

    async def async_turn_off(self, **kwargs):
        """Turn the switch off."""
        station_payload = []
        self._is_on = False
        await self._send_station_message(station_payload)

class BHyveRainDelaySwitch(BHyveDeviceEntity, SwitchEntity):
    """Define a BHyve rain delay switch."""

    def __init__(self, hass, bhyve, device, icon):
        """Initialize the switch."""
        name = "Rain Delay {}".format(device.get("name"))
        _LOGGER.info("Creating switch: %s", name)

        super().__init__(hass, bhyve, device, name, icon, DEVICE_CLASS_SWITCH)

    def _setup(self, device):
        self._is_on = False
        self._attrs = {
            "device_id": self._device_id,
        }
        self._available = device.get("is_connected", False)

        device_status = device.get("status")
        rain_delay = device_status.get("rain_delay")

        self._update_device_cb = None
        self._extract_rain_delay(rain_delay, device_status)

    def _on_ws_data(self, data):
        """
            {'event': 'rain_delay', 'device_id': 'id', 'delay': 0, 'timestamp': '2020-01-14T12:10:10.000Z'}
        """
        event = data.get("event")
        if event is None:
            _LOGGER.warning("No event on ws data {}".format(data))
            return
        elif event == EVENT_RAIN_DELAY:
            self._extract_rain_delay(
                data.get("delay"), {"rain_delay_started_at": data.get("timestamp")}
            )
            # The REST API returns more data about a rain delay (eg cause/weather_type)
            self._update_device_soon()

    def _should_handle_event(self, event_name):
        return event_name in [EVENT_RAIN_DELAY]

    def _update_device_soon(self):
        if self._update_device_cb is not None:
            self._update_device_cb()  # unsubscribe
        self._update_device_cb = async_call_later(self._hass, 1, self._update_device)

    async def _update_device(self, time):
        await self._refetch_device(force_update=True)
        self.async_schedule_update_ha_state()

    def _extract_rain_delay(self, rain_delay, device_status=None):
        if rain_delay is not None and rain_delay > 0:
            self._is_on = True
            self._attrs = {ATTR_DELAY: rain_delay}
            if device_status is not None:
                self._attrs.update(
                    {
                        ATTR_CAUSE: device_status.get("rain_delay_cause"),
                        ATTR_WEATHER_TYPE: device_status.get("rain_delay_weather_type"),
                        ATTR_STARTED_AT: orbit_time_to_local_time(
                            device_status.get("rain_delay_started_at")
                        ),
                    }
                )
        else:
            self._is_on = False
            self._attrs = {}

    async def _set_rain_delay(self, hours=24):
        try:
            # {event: "rain_delay", device_id: "5ae3c7884f0c72d7d626ba06", delay: 48}
            payload = {
                "event": EVENT_RAIN_DELAY,
                "device_id": self._device_id,
                "delay": hours,
            }
            _LOGGER.info("Setting rain delay: {}".format(payload))
            await self._bhyve.send_message(payload)

        except BHyveError as err:
            _LOGGER.warning("Failed to send to BHyve websocket message %s", err)
            raise (err)

    @property
    def is_on(self):
        """Return the status of the sensor."""
        return self._is_on

    @property
    def unique_id(self):
        """Return a unique, unchanging string that represents this sensor."""
        return f"{self._mac_address}:{self._device_type}:rain_delay:{self._device_id}"

    async def async_turn_on(self, **kwargs):
        """Turn the switch on."""
        self._is_on = True
        await self._set_rain_delay()

    async def async_turn_off(self, **kwargs):
        """Turn the switch off."""
        self._is_on = False
        await self._set_rain_delay(0)

