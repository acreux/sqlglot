import re
from enum import Enum


class AutoName(Enum):
    # pylint: disable=no-self-argument
    def _generate_next_value_(name, _start, _count, _last_values):
        return name


class RegisteringMeta(type):
    classes = {}

    @classmethod
    def __getitem__(cls, key):
        return cls.classes[key]

    @classmethod
    def get(cls, key, default):
        return cls.classes.get(key, default)

    def __new__(cls, clsname, bases, attrs):
        clazz = super().__new__(cls, clsname, bases, attrs)
        cls.classes[clsname.lower()] = clazz
        return clazz


def list_get(arr, index):
    return arr[index] if index < len(arr) else None


def ensure_list(value):
    return value if isinstance(value, list) else [value]


def csv(*args, sep=", "):
    return sep.join(arg for arg in args if arg)


CAMEL_CASE_PATTERN = re.compile("(?<!^)(?=[A-Z])")


def camel_to_snake_case(name):
    return CAMEL_CASE_PATTERN.sub("_", name).upper()
