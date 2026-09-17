# Modifying Resources

## Scale a Deployment

Scale the {py:class}`Depoyment <kr8s.objects.Deployment>` `metrics-server` using {py:func}`Deployment.scale() <kr8s.objects.Deployment.scale()>`
in the Namespace `kube-system` to `1` replica.

`````{tab-set}

````{tab-item} Sync
:sync: sync
```python
from kr8s.objects import Deployment

deploy = Deployment.get("metrics-server", namespace="kube-system")
deploy.scale(1)
```
````

````{tab-item} Async
:sync: async
```python
from kr8s.asyncio.objects import Deployment

deploy = await Deployment.get("metrics-server", namespace="kube-system")
await deploy.scale(1)
```
````

`````

## Add Pod label

Add the label `foo` with the value `bar` to an existing {py:class}`Pod <kr8s.objects.Pod>` using {py:func}`Pod.label() <kr8s.objects.Pod.label()>`.

`````{tab-set}

````{tab-item} Sync
:sync: sync
```python
from kr8s.objects import Pod

pod = Pod("kube-apiserver", namespace="kube-system")
pod.label({"foo": "bar"})
```
````

````{tab-item} Async
:sync: async
```python
from kr8s.asyncio.objects import Pod

pod = await Pod("kube-apiserver", namespace="kube-system")
await pod.label({"foo": "bar"})
```
````

`````

## Replace all Pod labels

Using the [JSON 6902](https://jsonpatch.com/) style patching replace all {py:class}`Pod <kr8s.objects.Pod>` labels with `{"patched": "true"}` using {py:func}`Pod.patch() <kr8s.objects.Pod.patch()>`.

`````{tab-set}

````{tab-item} Sync
:sync: sync
```python
from kr8s.objects import Pod

pod = Pod("my-pod", namespace="kube-system")
pod.patch(
    [{"op": "replace", "path": "/metadata/labels", "value": {"patched": "true"}}],
    type="json",
)
```
````

````{tab-item} Async
:sync: async
```python
from kr8s.asyncio.objects import Pod

pod = await Pod("my-pod", namespace="kube-system")
await pod.patch(
    [{"op": "replace", "path": "/metadata/labels", "value": {"patched": "true"}}],
    type="json",
)
```
````

`````

## Server-side apply a Pod

Declare the whole {py:class}`Pod <kr8s.objects.Pod>` with {py:func}`Pod.apply() <kr8s.objects.Pod.apply()>` instead of describing a change to it. The API server records `field_manager` as the owner of every field the object sets, and removes the fields that manager stops setting.

Unlike {py:func}`Pod.patch() <kr8s.objects.Pod.patch()>`, this creates the object when it does not exist yet.

`````{tab-set}

````{tab-item} Sync
:sync: sync
```python
from kr8s.objects import Pod

pod = Pod({
    "metadata": {"name": "my-pod", "labels": {"foo": "bar"}},
    "spec": {"containers": [{"name": "pause", "image": "registry.k8s.io/pause"}]},
})
pod.apply(field_manager="my-controller")
```
````

````{tab-item} Async
:sync: async
```python
from kr8s.asyncio.objects import Pod

pod = await Pod({
    "metadata": {"name": "my-pod", "labels": {"foo": "bar"}},
    "spec": {"containers": [{"name": "pause", "image": "registry.k8s.io/pause"}]},
})
await pod.apply(field_manager="my-controller")
```
````

`````

Pass `force=True` to take a field that another manager owns; without it the API server answers `409`. Pass `dry_run=True` to ask what the merge would produce without storing it.

## Cordon a Node

Cordon a {py:class}`Node <kr8s.objects.Node>` to mark it as unschedulable with {py:func}`Node.cordon() <kr8s.objects.Node.cordon()>`.

`````{tab-set}

````{tab-item} Sync
:sync: sync
```python
from kr8s.objects import Node

node = Node("k8s-node-1")

node.cordon()
```
````

````{tab-item} Async
:sync: async
```python
from kr8s.asyncio.objects import Node

node = await Node("k8s-node-1")

await node.cordon()
```
````

`````
