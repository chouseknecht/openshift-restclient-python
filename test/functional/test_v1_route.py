# -*- coding: utf-8 -*-
from __future__ import absolute_import
from __future__ import print_function

import copy



def test_create_route(ansible_helper, create_tasks, obj_compare, create_namespace):
    parameters = create_tasks['create']
    new_obj = ansible_helper.object_from_params(parameters)
    namespace = parameters.get('namespace')
    if namespace:
        create_namespace(namespace)
    k8s_obj = ansible_helper.create_object(namespace, new_obj, wait=True)
    obj_compare(ansible_helper, k8s_obj, parameters)


def test_get_route(ansible_helper, create_tasks):
    parameters = create_tasks['create']
    namespace = parameters.get('namespace')
    k8s_obj = ansible_helper.get_object(parameters['name'], namespace)
    assert k8s_obj is not None


def test_patch_route(ansible_helper, patch_tasks, obj_compare):
    parameters = patch_tasks['patch']
    name = parameters['name']
    namespace = parameters.get('namespace')
    existing_obj = ansible_helper.get_object(name, namespace)
    updated_obj = copy.deepcopy(existing_obj)
    ansible_helper.object_from_params(parameters, obj=updated_obj)
    match = ansible_helper.objects_match(existing_obj, updated_obj)
    assert not match
    new_obj = ansible_helper.patch_object(name, namespace, updated_obj, wait=True)
    assert new_obj is not None
    obj_compare(ansible_helper, new_obj, parameters)


def test_replace_route(ansible_helper, replace_tasks, obj_compare):
    parameters = replace_tasks['replace']
    name = parameters.get('name')
    namespace = parameters.get('namespace')
    existing_obj = ansible_helper.get_object(name, namespace)
    ansible_helper.object_from_params(parameters, obj=existing_obj)
    k8s_obj = ansible_helper.replace_object(name, namespace, existing_obj, wait=True)
    obj_compare(ansible_helper, k8s_obj, parameters)


def test_remove_route(ansible_helper, create_tasks):
    parameters = create_tasks['create']
    namespace = parameters.get('namespace')
    name = parameters.get('name')
    ansible_helper.delete_object(name, namespace, wait=True)
    k8s_obj = ansible_helper.get_object(name, namespace)
    assert k8s_obj is None


def test_remove_namespace(namespaces, delete_namespace):
    k8s_obj = delete_namespace(namespaces)
    assert k8s_obj is None
