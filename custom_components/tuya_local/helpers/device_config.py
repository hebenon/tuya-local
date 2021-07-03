"""
Config parser for Tuya Local devices.
"""
from fnmatch import fnmatch
import logging
from os import walk
from os.path import join, dirname, splitext
from pydoc import locate

from homeassistant.util.yaml import load_yaml

import custom_components.tuya_local.devices as config_dir

_LOGGER = logging.getLogger(__name__)


def _typematch(type, value):
    # Workaround annoying legacy of bool being a subclass of int in Python
    if type is int and isinstance(value, bool):
        return False

    if isinstance(value, type):
        return True
    # Allow values embedded in strings if they can be converted
    # But not for bool, as everything can be converted to bool
    elif isinstance(value, str) and type is not bool:
        try:
            type(value)
            return True
        except ValueError:
            return False
    return False


class TuyaDeviceConfig:
    """Representation of a device config for Tuya Local devices."""

    def __init__(self, fname):
        """Initialize the device config.
        Args:
            fname (string): The filename of the yaml config to load."""
        _CONFIG_DIR = dirname(config_dir.__file__)
        self._fname = fname
        filename = join(_CONFIG_DIR, fname)
        self._config = load_yaml(filename)
        _LOGGER.debug("Loaded device config %s", fname)

    @property
    def name(self):
        """Return the friendly name for this device."""
        return self._config["name"]

    @property
    def config(self):
        """Return the config file associated with this device."""
        return self._fname

    @property
    def legacy_type(self):
        """Return the legacy conf_type associated with this device."""
        return self._config.get("legacy_type", splitext(self.config)[0])

    @property
    def primary_entity(self):
        """Return the primary type of entity for this device."""
        return TuyaEntityConfig(self, self._config["primary_entity"])

    def secondary_entities(self):
        """Iterate through entites for any secondary entites supported."""
        for conf in self._config.get("secondary_entities", {}):
            yield TuyaEntityConfig(self, conf)

    def matches(self, dps):
        """Determine if this device matches the provided dps map."""
        for d in self.primary_entity.dps():
            if d.id not in dps.keys() or not _typematch(d.type, dps[d.id]):
                return False

        for dev in self.secondary_entities():
            for d in dev.dps():
                if d.id not in dps.keys() or not _typematch(d.type, dps[d.id]):
                    return False
        _LOGGER.debug("Matched config for %s", self.name)
        return True

    def _entity_match_analyse(self, entity, keys, matched, dps):
        """
        Determine whether this entity can be a match for the dps
          Args:
            entity - the TuyaEntityConfig to check against
            keys - the unmatched keys for the device
            matched - the matched keys for the device
            dps - the dps values to be matched
        Side Effects:
            Moves items from keys to matched if they match dps
        Return Value:
            True if all dps in entity could be matched to dps, False otherwise
        """
        for d in entity.dps():
            if (d.id not in keys and d.id not in matched) or not _typematch(
                d.type, dps[d.id]
            ):
                return False
            if d.id in keys:
                matched.append(d.id)
                keys.remove(d.id)
        return True

    def match_quality(self, dps):
        """Determine the match quality for the provided dps map."""
        keys = list(dps.keys())
        matched = []
        if "updated_at" in keys:
            keys.remove("updated_at")
        total = len(keys)
        if not self._entity_match_analyse(self.primary_entity, keys, matched, dps):
            return 0

        for e in self.secondary_entities():
            if not self._entity_match_analyse(e, keys, matched, dps):
                return 0

        return round((total - len(keys)) * 100 / total)


class TuyaEntityConfig:
    """Representation of an entity config for a supported entity."""

    def __init__(self, device, config):
        self._device = device
        self._config = config

    @property
    def name(self):
        """The friendly name for this entity."""
        own_name = self._config.get("name")
        if own_name is None:
            return self._device.name
        else:
            return self._device.name + " " + own_name

    @property
    def legacy_class(self):
        """Return the legacy device corresponding to this config."""
        name = self._config.get("legacy_class")
        if name is None:
            return None
        return locate("custom_components.tuya_local" + name)

    @property
    def deprecated(self):
        """Return whether this entitiy is deprecated."""
        return "deprecated" in self._config.keys()

    @property
    def deprecation_message(self):
        """Return a deprecation message for this entity"""
        replacement = self._config.get(
            "deprecated", "nothing, this warning has been raised in error"
        )
        return (
            f"The use of {self.entity} for {self._device.name} is "
            f"deprecated and should be replaced by {replacement}."
        )

    @property
    def entity(self):
        """The entity type of this entity."""
        return self._config["entity"]

    @property
    def device_class(self):
        """The device class of this entity."""
        return self._config.get("class")

    def icon(self, device):
        """Return the icon for this device, with state as given."""
        icon = self._config.get("icon", None)
        priority = self._config.get("icon_priority", 100)

        for d in self.dps():
            rule = d.icon_rule(device)
            if rule and rule["priority"] < priority:
                icon = rule["icon"]
                priority = rule["priority"]
        return icon

    def dps(self):
        """Iterate through the list of dps for this entity."""
        for d in self._config["dps"]:
            yield TuyaDpsConfig(self, d)

    def find_dps(self, name):
        """Find a dps with the specified name."""
        for d in self.dps():
            if d.name == name:
                return d
        return None


class TuyaDpsConfig:
    """Representation of a dps config."""

    def __init__(self, entity, config):
        self._entity = entity
        self._config = config

    @property
    def id(self):
        return str(self._config["id"])

    @property
    def type(self):
        t = self._config["type"]
        types = {
            "boolean": bool,
            "integer": int,
            "string": str,
            "float": float,
            "bitfield": int,
        }
        return types.get(t)

    @property
    def name(self):
        return self._config["name"]

    def get_value(self, device):
        """Return the value of the dps from the given device."""
        return self._map_from_dps(device.get_property(self.id), device)

    async def async_set_value(self, device, value):
        """Set the value of the dps in the given device to given value."""
        if self.readonly:
            raise TypeError(f"{self.name} is read only")
        if self.invalid_for(value, device):
            raise AttributeError(f"{self.name} cannot be set at this time")

        settings = self.get_values_to_set(device, value)
        await device.async_set_properties(settings)

    @property
    def values(self):
        """Return the possible values a dps can take."""
        if "mapping" not in self._config.keys():
            return None
        val = []
        for m in self._config["mapping"]:
            if "value" in m:
                val.append(m["value"])
            for c in m.get("conditions", {}):
                if "value" in c:
                    val.append(c["value"])

        return list(set(val)) if len(val) > 0 else None

    def range(self, device):
        """Return the range for this dps if configured."""
        mapping = self._find_map_for_dps(device.get_property(self.id))
        if mapping is not None:
            _LOGGER.debug(f"Considering mapping for range of {self.name}")
            cond = self._active_condition(mapping, device)
            if cond is not None:
                constraint = mapping.get("constraint")
                _LOGGER.debug(f"Considering condition on {constraint}")
            r = None if cond is None else cond.get("range")
            if r is not None and "min" in r and "max" in r:
                _LOGGER.info(f"Conditional range returned for {self.name}")
                return r
            r = mapping.get("range")
            if r is not None and "min" in r and "max" in r:
                _LOGGER.info(f"Mapped range returned for {self.name}")
                return r
        r = self._config.get("range")
        if r is not None and "min" in r and "max" in r:
            return r
        else:
            return None

    def step(self, device):
        step = 1
        scale = 1
        mapping = self._find_map_for_dps(device.get_property(self.id))
        if mapping is not None:
            _LOGGER.debug(f"Considering mapping for step of {self.name}")
            step = mapping.get("step", 1)
            scale = mapping.get("scale", 1)
            cond = self._active_condition(mapping, device)
            if cond is not None:
                constraint = mapping.get("constraint")
                _LOGGER.debug(f"Considering condition on {constraint}")
                step = cond.get("step", step)
                scale = cond.get("scale", scale)
        if step != 1 or scale != 1:
            _LOGGER.info(f"Step for {self.name} is {step} with scale {scale}")
        return step / scale

    @property
    def readonly(self):
        return self._config.get("readonly", False)

    @property
    def stringify(self):
        return self._config.get("stringify", False)

    def invalid_for(self, value, device):
        mapping = self._find_map_for_value(value)
        if mapping is not None:
            cond = self._active_condition(mapping, device)
            if cond is not None:
                return cond.get("invalid", False)
        return False

    @property
    def hidden(self):
        return self._config.get("hidden", False)

    def _find_map_for_dps(self, value):
        default = None
        for m in self._config.get("mapping", {}):
            if "dps_val" not in m:
                default = m
            elif str(m["dps_val"]) == str(value):
                return m
        return default

    def _map_from_dps(self, value, device):
        # For stringified dps, convert to desired type if possible
        if self.stringify and value is not None:
            try:
                value = self.type(value)
            except ValueError:
                pass
        result = value
        mapping = self._find_map_for_dps(value)
        if mapping is not None:
            scale = mapping.get("scale", 1)
            if not isinstance(scale, (int, float)):
                scale = 1
            redirect = mapping.get("value-redirect")
            replaced = "value" in mapping
            result = mapping.get("value", result)
            cond = self._active_condition(mapping, device)
            if cond is not None:
                if cond.get("invalid", False):
                    return None
                replaced = replaced or "value" in cond
                result = cond.get("value", result)
                scale = cond.get("scale", scale)
                redirect = cond.get("value-redirect", redirect)

                for m in cond.get("mapping", {}):
                    if str(m.get("dps_val")) == str(result):
                        replaced = "value" in m
                        result = m.get("value", result)

            if redirect is not None:
                _LOGGER.debug(f"Redirecting {self.name} to {redirect}")
                r_dps = self._entity.find_dps(redirect)
                return r_dps.get_value(device)

            if scale != 1 and isinstance(result, (int, float)):
                result = result / scale
                replaced = True

            if replaced:
                _LOGGER.debug(
                    "%s: Mapped dps %s value from %s to %s",
                    self._entity._device.name,
                    self.id,
                    value,
                    result,
                )

        return result

    def _find_map_for_value(self, value):
        default = None
        for m in self._config.get("mapping", {}):
            if "dps_val" not in m:
                default = m
            if "value" in m and str(m["value"]) == str(value):
                return m
            for c in m.get("conditions", {}):
                if "value" in c and c["value"] == value:
                    return m
        return default

    def _active_condition(self, mapping, device):
        constraint = mapping.get("constraint")
        conditions = mapping.get("conditions")
        if constraint is not None and conditions is not None:
            c_dps = self._entity.find_dps(constraint)
            c_val = None if c_dps is None else device.get_property(c_dps.id)
            if c_val is not None:
                for cond in conditions:
                    if c_val == cond.get("dps_val"):
                        return cond
        return None

    def get_values_to_set(self, device, value):
        """Return the dps values that would be set when setting to value"""
        result = value
        dps_map = {}
        mapping = self._find_map_for_value(value)
        if mapping is not None:
            replaced = False
            scale = mapping.get("scale", 1)
            redirect = mapping.get("value-redirect")
            if not isinstance(scale, (int, float)):
                scale = 1
            step = mapping.get("step")
            if not isinstance(step, (int, float)):
                step = None
            if "dps_val" in mapping:
                result = mapping["dps_val"]
                replaced = True
            # Conditions may have side effect of setting another value.
            cond = self._active_condition(mapping, device)
            if cond is not None:
                if cond.get("value") == value:
                    c_dps = self._entity.find_dps(mapping["constraint"])
                    dps_map.update(c_dps.get_values_to_set(device, cond["dps_val"]))
                scale = cond.get("scale", scale)
                step = cond.get("step", step)
                redirect = cond.get("value-redirect", redirect)

            if redirect is not None:
                _LOGGER.debug(f"Redirecting {self.name} to {redirect}")
                r_dps = self._entity.find_dps(redirect)
                return r_dps.get_values_to_set(device, value)

            if scale != 1 and isinstance(result, (int, float)):
                _LOGGER.debug(f"Scaling {result} by {scale}")
                result = result * scale
                replaced = True

            if step is not None and isinstance(result, (int, float)):
                _LOGGER.debug(f"Stepping {result} to {step}")
                result = step * round(float(result) / step)
                replaced = True

            if replaced:
                _LOGGER.debug(
                    "%s: Mapped dps %s to %s from %s",
                    self._entity._device.name,
                    self.id,
                    result,
                    value,
                )

        r = self.range(device)
        if r is not None:
            minimum = r["min"]
            maximum = r["max"]
            if result < minimum or result > maximum:
                raise ValueError(
                    f"{self.name} ({value}) must be between {minimum} and {maximum}"
                )

        if self.type is int:
            _LOGGER.debug(f"Rounding {self.name}")
            result = int(round(result))
        elif self.type is bool:
            result = True if result else False
        elif self.type is float:
            result = float(result)
        elif self.type is str:
            result = str(result)

        if self.stringify:
            result = str(result)

        dps_map[self.id] = result
        return dps_map

    def icon_rule(self, device):
        mapping = self._find_map_for_dps(device.get_property(self.id))
        icon = None
        priority = 100
        if mapping:
            icon = mapping.get("icon", icon)
            priority = mapping.get("icon_priority", 10 if icon else 100)
            cond = self._active_condition(mapping, device)
            if cond and cond.get("icon_priority", 10) < priority:
                icon = cond.get("icon", icon)
                priority = cond.get("icon_priority", 10 if icon else 100)

        return {"priority": priority, "icon": icon}


def available_configs():
    """List the available config files."""
    _CONFIG_DIR = dirname(config_dir.__file__)

    for (path, dirs, files) in walk(_CONFIG_DIR):
        for basename in sorted(files):
            if fnmatch(basename, "*.yaml"):
                yield basename


def possible_matches(dps):
    """Return possible matching configs for a given set of dps values."""
    for cfg in available_configs():
        parsed = TuyaDeviceConfig(cfg)
        if parsed.matches(dps):
            yield parsed


def config_for_legacy_use(conf_type):
    """
    Return a config to use with config_type for legacy transition.
    Note: as there are two variants for Kogan Socket, this is not guaranteed
    to be the correct config for the device, so only use it for looking up
    the legacy class during the transition period.
    """
    for cfg in available_configs():
        parsed = TuyaDeviceConfig(cfg)
        if parsed.legacy_type == conf_type:
            return parsed

    return None
