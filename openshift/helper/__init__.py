# -*- coding: utf-8 -*-
from __future__ import absolute_import

import inspect
import json
import logging
import math
import os
import re
import time

import string_utils

from logging import config as logging_config

from kubernetes import config
from kubernetes.config.config_exception import ConfigException
from kubernetes.client.rest import ApiException

from openshift import client
from openshift.client.models import V1DeleteOptions

from .exceptions import OpenShiftException

# Regex for finding versions
VERSION_RX = re.compile("V\d((alpha|beta)\d)?")

BASE_API_VERSION = 'V1'

logger = logging.getLogger(__name__)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': True,
    'handlers': {
        'file': {
            'level': 'DEBUG',
            'class': 'logging.FileHandler',
            'filename': 'KubeObjHelper.log',
            'mode': 'a',
            'encoding': 'utf-8'
        },
        'null': {
            'level': 'ERROR',
            'class': 'logging.NullHandler'
        }
    },
    'loggers': {
        'openshift.helper': {
            'handlers': ['file'],
            'level': 'INFO',
            'propagate': False
        },
    },
    'root': {
        'handlers': ['null'],
        'level': 'ERROR'
    }
}


class KubernetesObjectHelper(object):

    def __init__(self, api_version, kind, debug=False, reset_logfile=True):
        self.api_version = api_version
        self.kind = kind
        self.model = self.get_model(api_version, kind)
        self.properties = self.properties_from_model_obj(self.model())
        self.base_model_name = self.get_base_model_name(self.model.__name__)
        self.base_model_name_snake = self.get_base_model_name_snake(self.base_model_name)

        if debug:
            self.enable_debug(reset_logfile)

    @staticmethod
    def enable_debug(reset_logfile=True):
        """ Turn on debugging. If reset_logfile, then remove the existing log file. """
        if reset_logfile:
            LOGGING['loggers']['handlers']['file']['mode'] = 'w'
        LOGGING['loggers'][__name__]['level'] = 'DEBUG'
        logging_config.dictConfig(LOGGING)

    def set_client_config(self, module_params):
        """
        Handles loading client config from a file, or updating the config object with any user provided
        authentication data.

        :param module_params: dict of params from AnsibleModule
        :param module_arg_spec: dict containing the Ansible module argument_spec
        :return: None
        """
        if module_params.get('kubeconfig') or module_params.get('context'):
            # Attempt to load config from file
            try:
                config.load_kube_config(config_file=module_params.get('kubeconfig'),
                                        context=module_params.get('context'))
            except IOError as e:
                raise OpenShiftException("Failed to access {}. Does the file exist?".format(
                    module_params.get('kubeconfig')), error=str(e))
            except ConfigException as e:
                raise OpenShiftException("Error accessing context {}.".format(
                    module_params.get('context')), error=str(e))
        elif module_params.get('api_url') or module_params.get('api_key') or module_params.get('username'):
            # Anything in argspec with an auth_option attribute, can be copied
            for arg_name, arg_value in self.argspec.items():
                if arg_value.get('auth_option'):
                    if module_params.get(arg_name, None) is not None:
                        setattr(client.configuration, arg_name, module_params[arg_name])
        else:
            # The user did not pass any options, so load the default kube config file, and use the active
            # context
            try:
                config.load_kube_config()
            except Exception as e:
                raise OpenShiftException("Error loading configuration: {}".format(e))

    def get_object(self, name, namespace=None):
        k8s_obj = None
        try:
            get_method = self.__lookup_method('read', namespace)
            if namespace is None:
                k8s_obj = get_method(name)
            else:
                k8s_obj = get_method(name, namespace)
        except ApiException as exc:
            if exc.status != 404:
                msg = json.loads(exc.body).get('message', exc.reason)
                raise OpenShiftException(msg, status=exc.status)
        return k8s_obj

    def patch_object(self, name, namespace, k8s_obj, wait=False, timeout=60):
        # TODO: add a parameter for waiting until the object is ready
        empty_status = self.properties['status']['class']()
        k8s_obj.status = empty_status
        k8s_obj.metadata.resource_version = None
        self.__remove_creation_timestamps(k8s_obj)
        logger.debug("Patching object: {}".format(json.dumps(k8s_obj.to_dict(), indent=4)))
        try:
            patch_method = self.__lookup_method('patch', namespace)
            if namespace:
                return_obj = patch_method(name, namespace, k8s_obj)
            else:
                return_obj = patch_method(name, k8s_obj)
        except ApiException as exc:

            msg = json.loads(exc.body).get('message', exc.reason)
            raise OpenShiftException(str(exc))

        if wait:
            # wait for the object to be ready
            tries = 0
            half = math.ceil(timeout / 2)
            while tries <= half:
                obj = self.get_object(name, namespace)
                if hasattr(obj.status, 'phase'):
                    return_obj = obj
                    if obj.status.phase == 'Active':
                        break
                elif obj is not None:
                    # TODO: is there a better way?
                    # if the object exists, then assume it's ready?
                    return_obj = obj
                    break
                tries += 2
                time.sleep(2)

        return return_obj

    def create_object(self, namespace, k8s_obj, wait=False, timeout=60):
        # TODO: add a parameter for waiting until the object is ready
        try:
            create_method = self.__lookup_method('create', namespace)
            if namespace is None:
                return_obj = create_method(k8s_obj)
            else:
                return_obj = create_method(namespace, k8s_obj)
        except ApiException as exc:
            msg = json.loads(exc.body).get('message', exc.reason)
            raise OpenShiftException(msg, status=exc.status)

        if wait:
            # wait for the object to be ready
            tries = 0
            half = math.ceil(timeout / 2)
            while tries <= half:
                obj = self.get_object(k8s_obj.metadata.name, namespace)
                if hasattr(obj.status, 'phase'):
                    return_obj = obj
                    if obj.status.phase == 'Active':
                        break
                elif obj is not None:
                    # TODO: is there a better way?
                    # if the object exists, then assume it's ready?
                    return_obj = obj
                    break
                tries += 2
                time.sleep(2)

        return return_obj

    def delete_object(self, name, namespace, wait=False, timeout=60):
        delete_method = self.__lookup_method('delete', namespace)
        if not namespace:
            try:
                if 'body' in inspect.getargspec(delete_method).args:
                    delete_method(name, body=V1DeleteOptions())
                else:
                    delete_method(name)
            except ApiException as exc:
                msg = json.loads(exc.body).get('message', exc.reason)
                raise OpenShiftException(msg, status=exc.status)
        else:
            try:
                if 'body' in inspect.getargspec(delete_method).args:
                    delete_method(name, namespace, body=V1DeleteOptions())
                else:
                    delete_method(name, namespace)
            except ApiException as exc:
                msg = json.loads(exc.body).get('message', exc.reason)
                raise OpenShiftException(msg, status=exc.status)

        if wait:
            # wait for the object to be removed
            tries = 0
            half = math.ceil(timeout / 2)
            while tries <= half:
                obj = self.get_object(name, namespace)
                if not obj:
                    break
                tries += 2
                time.sleep(2)

    def update_object(self, name, namespace, k8s_obj):
        pass

    def objects_match(self, obj_a, obj_b):
        """ Test the equality of two objects. """
        if obj_a is None and obj_b is None:
            return True
        if not obj_a or not obj_b:
            return False
        if type(obj_a).__name__ != type(obj_b).__name__:
            return False
        dict_a = obj_a.to_dict()
        dict_b = obj_b.to_dict()
        return self.__match_dict(dict_a, dict_b)

    def __match_dict(self, dict_a, dict_b):
        if not dict_a and not dict_b:
            return True
        if not dict_a or not dict_b:
            return False
        match = True
        for key_a, value_a in dict_a.items():
            if key_a not in dict_b:
                logger.debug("obj_compare: {0} not found in {1}".format(key_a, dict_b))
                match = False
                break
            elif value_a is None and dict_b[key_a] is None:
                continue
            elif value_a is None or dict_b[key_a] is None:
                logger.debug("obj_compare: {0}:{1} !=  {2}:{3}".format(key_a, value_a, key_a, dict_b[key_a]))
                match = False
                break
            elif type(value_a).__name__ == 'list':
                sub_match = self.__match_list(value_a, dict_b[key_a])
                if not sub_match:
                    match = False
                    logger.debug("obj_compare: {0}:{1} !=  {2}:{3}".format(key_a, value_a, key_a, dict_b[key_a]))
                    break
            elif type(value_a).__name__ == 'dict':
                sub_match = self.__match_dict(value_a, dict_b[key_a])
                if not sub_match:
                    match = False
                    logger.debug("obj_compare: {0}:{1} !=  {2}:{3}".format(key_a, value_a, key_a, dict_b[key_a]))
                    break
            elif value_a != dict_b[key_a]:
                logger.debug("obj_compare: {0}:{1} !=  {2}:{3}".format(key_a, value_a, key_a, dict_b[key_a]))
                match = False
                break
        return match

    def __match_list(self, list_a, list_b):
        if not list_a and not list_b:
            return True
        if not list_a or not list_b:
            return False
        match = True
        if type(list_a[0]).__name__ == 'dict':
            for item_a in list_a:
                found = False
                for item_b in list_b:
                    if '__cmp__' in dir(item_b):
                        if item_a == item_b:
                            found = True
                            break
                    else:
                        if item_a.items() == item_b.items():
                            found = True
                            break
                if not found:
                    match = False
                    break
        elif type(list_a[0]).__name__ == 'list':
            for item_a in list_a:
                found = False
                for item_b in list_b:
                    sub_match = self.__match_list(item_a, item_b)
                    if sub_match:
                        found = True
                        break
                if not found:
                    match = False
                    break
        else:
            if set(list_a) != set(list_b):
                match = False
        return match

    @classmethod
    def properties_from_model_obj(cls, model_obj):
        """
        Introspect an object, and return a dict of 'name:dict of properties' pairs. The properties include: class,
        and immutable (a bool).

        :param model_obj: An object instantiated from openshift.client.models
        :return: dict
        """
        model_class = type(model_obj)

        # Create a list of model properties. Each property is represented as a dict of key:value pairs
        #  If a property does not have a setter, it's considered to be immutable
        properties = [
            {'name': x,
             'immutable': False if getattr(getattr(model_class, x), 'setter', None) else True
             }
            for x in dir(model_class) if isinstance(getattr(model_class, x), property)
        ]

        result = {}
        for prop in properties:
            prop_kind = model_obj.swagger_types[prop['name']]
            if prop_kind in ('str', 'int', 'bool'):
                prop_class = eval(prop_kind)
            elif prop_kind.startswith('list['):
                prop_class = list
            elif prop_kind.startswith('dict('):
                prop_class = dict
            else:
                prop_class = getattr(client.models, prop_kind)
            result[prop['name']] = {
                'class': prop_class,
                'immutable': prop['immutable']
            }
        return result

    def __lookup_method(self, operation, namespace=None):
        """
        Get the requested method (e.g. create, delete, patch, update) for
        the model object.
        :param operation: one of create, delete, patch, update
        :param namespace: optional name of the namespace.
        :return: pointer to the method
        """
        method_name = operation
        method_name += '_namespaced_' if namespace else '_'
        method_name += self.kind

        apis = [x for x in dir(client.apis) if VERSION_RX.search(x)]
        apis.append('OapiApi')

        method = None
        for api in apis:
            api_class = getattr(client.apis, api)
            method = getattr(api_class(), method_name, None)
            if method is not None:
                break
        if method is None:
            msg = "Did you forget to include the namespace?" if not namespace else ""
            raise OpenShiftException(
                "Error: method {0} not found for model {1}. {2}".format(method_name, self.kind, msg)
            )
        return method

    @staticmethod
    def get_base_model_name(model_name):
        """
        Return model_name with API Version removed.
        :param model_name: string
        :return: string
        """
        return VERSION_RX.sub('', model_name)

    def get_base_model_name_snake(self, model_name):
        """
        Return base model name with API version removed, and remainder converted to snake case
        :param model_name: string
        :return: string
        """
        result = self.get_base_model_name(model_name)
        return string_utils.camel_case_to_snake(result)

    @staticmethod
    def get_model(api_version, kind):
        """
        Return the model class for the requested object.

        :param api_version: API version string
        :param kind: The name of object type (i.e. Service, Route, Container, etc.)
        :return: class
        """
        camel_kind = string_utils.snake_case_to_camel(kind)
        # capitalize the first letter of the string without lower-casing the remainder
        name = camel_kind[:1].capitalize() + camel_kind[1:]
        model_name = api_version.capitalize() + name
        try:
            model = getattr(client.models, model_name)
        except Exception:
            raise OpenShiftException(
                    "Error: openshift.client.models.{} was not found. "
                    "Did you specify the correct Kind and API Version?".format(model_name)
            )
        return model

    def __remove_creation_timestamps(self, obj):
        """ Recursively look for creation_timestamp property, and set it to None """
        if hasattr(obj, 'swagger_types'):
            for key, value in obj.swagger_types.items():
                if key == 'creation_timestamp':
                    obj.creation_timestamp = None
                    continue
                if value.startswith('dict(') or value.startswith('list['):
                    continue
                if value in ('str', 'int', 'bool'):
                    continue
                if getattr(obj, key) is not None:
                    self.__remove_creation_timestamps(getattr(obj, key))
