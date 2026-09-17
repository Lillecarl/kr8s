# Modifying Resources

## Modify fields with Apply

Apply a resource with {py:func}`Deployment.apply() <kr8s.objects.Deployment.apply()>`. The object itself is the request body, so a partial one updates only the fields it sets — here the memory limit of a deployment. Containers are merged by name, so `name` has to be there for the right one to be found.

`````{tab-set}

````{tab-item} Sync
:sync: sync
```python
from kr8s.objects import Deployment

deploy = Deployment({"metadata": {"name": "my-deployment"}, "spec": {"template": {"spec": {"containers": [{"name": "alpine", "resources": {"limits": {"memory": "5Gi"}}}]}}}})
deploy.apply()
```
````

````{tab-item} Async
:sync: async
```python
from kr8s.asyncio.objects import Deployment

deploy = await Deployment({"metadata": {"name": "my-deployment"}, "spec": {"template": {"spec": {"containers": [{"name": "alpine", "resources": {"limits": {"memory": "5Gi"}}}]}}}})
await deploy.apply()
```
````

`````

## Manage specific fields with Server-Side Apply

[Server-Side Apply](https://kubernetes.io/docs/reference/using-api/server-side-apply/) allows for fine-grained management of specific fields.

`````{tab-set}

````{tab-item} Sync
:sync: sync
```python
from kr8s.objects import Deployment

deploy = Deployment({"metadata": {"name": "my-deployment"}, "spec": {"template": {"spec": {"containers": [{"name": "alpine", "resources": {"limits": {"memory": "5Gi"}}}]}}}})
deploy.apply(server_side=True)
```
````

````{tab-item} Async
:sync: async
```python
from kr8s.asyncio.objects import Deployment

deploy = await Deployment({"metadata": {"name": "my-deployment"}, "spec": {"template": {"spec": {"containers": [{"name": "alpine", "resources": {"limits": {"memory": "5Gi"}}}]}}}})
await deploy.apply(server_side=True)
```
````

`````

## Check an apply without storing it

Pass `dry_run="server"` to ask the API server what the apply would do. It runs
validation and admission controllers and then discards the result, so nothing is
stored and the object is left as it was.

`````{tab-set}

````{tab-item} Sync
:sync: sync
```python
from kr8s.objects import Deployment

deploy = Deployment.get("my-deployment")
deploy.apply(dry_run="server")
```
````

````{tab-item} Async
:sync: async
```python
from kr8s.asyncio.objects import Deployment

deploy = await Deployment.get("my-deployment")
await deploy.apply(dry_run="server")
```
````

`````

`kubectl` also has `--dry-run=client`, which prints the object it would have
sent. There is nothing to print here, so it is not accepted: asking for a dry
run and getting a real write would be the worst outcome available.

## Patch Resources

Use {py:func}`Deployment.patch() <kr8s.objects.Deployment.patch()>` to patch a resource with a JSON 6902 patch. This is useful for making small changes to a resource, such as updating the image of a deployment. `type="json"` selects that patch format; without it the body is sent as a merge patch.

`````{tab-set}

````{tab-item} Sync
:sync: sync
```python
from kr8s.objects import Deployment

deploy = Deployment.get("my-deployment")
deploy.patch(
    [{"op": "replace", "path": "/spec/template/spec/containers/0/image", "value": "my-app:latest"}],
    type="json",
)
```
````

````{tab-item} Async
:sync: async
```python
from kr8s.asyncio.objects import Deployment

deploy = await Deployment.get("my-deployment")
await deploy.patch(
    [{"op": "replace", "path": "/spec/template/spec/containers/0/image", "value": "my-app:latest"}],
    type="json",
)
```
````

`````

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
