#!/usr/bin/env python
import logging
import os
import yaml
from collections import defaultdict 


logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)


"""
1. Configuration definitions
2. "Scenario" setups (varying defaults)
3. Validation of actual values

{
    "DD_TRACE_RATE_LIMIT":{
        "value": 100,
        "source": {
            "type": "environment variable",
            "name": "DD_TRACE_RATE_LIMIT"
        },
        "alternatives": [
            {
                "type": "schema",
                "name": "defaults.yaml"
            }
        ],
        "schema": {
            "description": "...",
            "default": 100,
        }
    }
}
"""

class ConfigurationOptionSource:
    def __init__(self, name, value):
        self.name = name
        self.value = value
    
    def __repr__(self):
        return f"({self.name}: {self.value})"


class ConfigurationOption:
    def __init__(self, schema):
        self._sources = []
        self.value = None
        self.schema = schema

    def update(self, value, source="manual"):
        self.value = value
        self._sources.append(ConfigurationOptionSource(source, value))

    def __repr__(self):
        #source_string = [str(source) for source in self._sources])
        return f"ConfigurationOption(Value: {self.value}, sources: {self._sources})"


def _load_schema(filename):
    log.info("Reading file: %s", filename)
    file = None

    with open(filename) as f:
        file =  yaml.safe_load(f)

    log.info("Read file successfully: %s", filename)
    return file

def _load_configuration_value(name):
    if name not in os.environ:
        return ValueError("Configuration option not defined")
    return os.environ[name]


CONFIG = dict()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", help="The configuration schema to read from", required=True)
    args = parser.parse_args()

    config_schema = _load_schema(args.schema)

    for config_option, config_definition in config_schema.items():
        log.info("Processing config option: %s", config_option)
        config = ConfigurationOption(config_definition)
        config.update(config_definition["default"], source=args.schema)
        CONFIG[config_option] = config

    log.info("Configuration loaded: %s", CONFIG)