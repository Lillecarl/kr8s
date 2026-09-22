# SPDX-FileCopyrightText: Copyright (c) 2023-2026, Kr8s Developers (See LICENSE for list)
# SPDX-License-Identifier: BSD 3-Clause License
import copy
import importlib
import logging
import queue
import sys
import threading
import warnings
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import anyio
import httpx
import pytest
import yaml
from packaging.version import parse as parse_version

import kr8s
import kr8s.asyncio
from kr8s._api import _server_error
from kr8s._async_utils import anext
from kr8s._constants import (
    KUBERNETES_MAXIMUM_SUPPORTED_VERSION,
    KUBERNETES_MINIMUM_SUPPORTED_VERSION,
)
from kr8s._exceptions import APITimeoutError, ExecError, ServerError
from kr8s.asyncio.objects import Pod, Service, Table, new_class
from kr8s.objects import Pod as SyncPod
from kr8s.objects import Service as SyncService

if sys.version_info < (3, 11):
    from exceptiongroup import BaseExceptionGroup


@pytest.mark.parametrize(
    "status_code, body, message, status_type",
    [
        (409, {"kind": "Status", "message": "already exists"}, "already exists", dict),
        (500, {"kind": "Status", "message": "webhook denied"}, "webhook denied", dict),
        (502, "<html>Bad Gateway</html>", "boom", str),
        (503, "", "boom", str),
        (500, {"kind": "Status", "reason": "InternalError"}, "boom", str),
    ],
)
def test_server_error_reads_the_body_whatever_the_status_class(
    status_code, body, message, status_type
):
    kwargs = {"json": body} if isinstance(body, dict) else {"text": body}
    response = httpx.Response(
        status_code,
        request=httpx.Request("GET", "https://kubernetes/api/v1"),
        **kwargs,
    )
    error = httpx.HTTPStatusError("boom", request=response.request, response=response)
    translated = _server_error(error)
    assert str(translated) == message
    assert isinstance(translated.status, status_type)


@pytest.mark.parametrize("httpx_ws_version, unwrap", [("0.8", False), ("0.9", True)])
def test_httpx_ws_exceptions(httpx_ws_version, unwrap):
    api_module = importlib.import_module("kr8s._api")
    error = ExecError("command failed")
    exception_group = BaseExceptionGroup("websocket errors", [error])
    expected = error if unwrap else exception_group

    with patch.object(api_module, "_HTTPX_WS_VERSION", parse_version(httpx_ws_version)):
        with pytest.raises(type(expected)) as exc_info:
            with api_module._httpx_ws_exception_fixer():
                raise exception_group

    assert exc_info.value is expected


@pytest.fixture
async def example_crd(example_crd_spec):
    async with create_delete_crd(example_crd_spec) as example:
        yield example


@asynccontextmanager
async def create_delete_crd(spec):
    example = await kr8s.asyncio.objects.CustomResourceDefinition(spec)

    # Clean up any existing CRD if it exists from a previous failed test run
    if await example.exists():
        await example.delete()
    while await example.exists():
        await anyio.sleep(0.1)

    # Create the CRD
    if not await example.exists():
        await example.create()
    while not await example.exists():
        await anyio.sleep(0.1)

    # Check that the CRD gets returned
    assert example in [
        crd async for crd in kr8s.asyncio.get("customresourcedefinitions")
    ]
    yield example

    # Clean up the CRD
    await example.delete()
    while await example.exists():
        await anyio.sleep(0.1)


async def test_factory_bypass() -> None:
    with pytest.raises(ValueError, match="kr8s.api()"):
        _ = kr8s.Api()
    _ = kr8s.api()


async def test_api_factory(serviceaccount) -> None:
    k1 = await kr8s.asyncio.api()
    k2 = await kr8s.asyncio.api()
    assert k1 is k2

    k3 = await kr8s.asyncio.api(serviceaccount=serviceaccount)
    k4 = await kr8s.asyncio.api(serviceaccount=serviceaccount)
    assert k1 is not k3
    assert k3 is k4

    p = await Pod({"metadata": {"name": "foo"}})
    assert p.api is k1
    assert p.api is not k3


def test_api_factory_threaded():
    assert len(kr8s.Api._instances) == 0

    q = queue.Queue()

    def run_in_thread(q):
        async def create_api(q):
            k = await kr8s.asyncio.api()
            q.put(k)

        anyio.run(create_api, q)

    t1 = threading.Thread(
        target=run_in_thread,
        args=(q,),
    )
    t2 = threading.Thread(
        target=run_in_thread,
        args=(q,),
    )
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    k1 = q.get()
    k2 = q.get()

    assert k1 is not k2
    assert type(k1) is type(k2)


def test_api_factory_multi_event_loop() -> None:
    assert len(kr8s.Api._instances) == 0

    async def create_api():
        return await kr8s.asyncio.api()

    k1 = anyio.run(create_api)
    k2 = anyio.run(create_api)
    assert k1 is not k2


async def test_api_factory_with_kubeconfig(k8s_cluster, serviceaccount) -> None:
    k1 = await kr8s.asyncio.api(kubeconfig=k8s_cluster.kubeconfig_path)
    k2 = await kr8s.asyncio.api(serviceaccount=serviceaccount)
    k3 = await kr8s.asyncio.api()
    assert k1 is not k2
    assert k3 is k1
    assert k3 is not k2

    p = await Pod({"metadata": {"name": "foo"}})
    assert p.api is k1

    p2 = await Pod({"metadata": {"name": "bar"}}, api=k2)
    assert p2.api is k2

    p3 = await Pod({"metadata": {"name": "baz"}}, api=k3)
    assert p3.api is k3
    assert p3.api is not k2


async def test_the_factory_checks_the_version_once():
    """`api()` returns a cached Api and awaits it again, so the check has to
    be per instance rather than per await. It costs a `/version` request, and
    a caller that asks for an Api per operation used to pay one every time."""
    api = await kr8s.asyncio.api()
    keep = api.async_version
    calls = 0

    async def counting_version():
        nonlocal calls
        calls += 1
        return await keep()

    api.async_version = counting_version
    try:
        for _ in range(3):
            again = await kr8s.asyncio.api()
            assert again is api
    finally:
        api.async_version = keep

    assert calls == 0, "a cached Api re-ran the version check"


async def test_a_failed_first_await_is_not_cached_as_ready():
    """`_ready` is set after the work, not before it, so authentication that
    fails is retried rather than remembered as done."""
    api = await kr8s.asyncio.api()
    api._ready = False
    boom = RuntimeError("the credentials are not there yet")

    with patch.object(api, "_check_version", side_effect=boom):
        with pytest.raises(RuntimeError, match="not there yet"):
            await api

    assert api._ready is False
    await api
    assert api._ready is True


def test_version_sync():
    api = kr8s.api()
    version = api.version()
    assert "major" in version


async def test_version_sync_in_async():
    api = kr8s.api()
    version = api.version()
    assert "major" in version


async def test_version() -> None:
    api = await kr8s.asyncio.api()
    version = await api.version()
    assert "major" in version


def test_helper_version() -> None:
    version = kr8s.version()
    assert "major" in version


async def test_concurrent_api_creation() -> None:
    async def get_api():
        api = await kr8s.asyncio.api()
        await api.version()

    async with anyio.create_task_group() as tg:
        for _ in range(10):
            tg.start_soon(get_api)


async def test_both_api_creation_methods_together():
    async_api = await kr8s.asyncio.api()
    api = kr8s.api()

    assert await kr8s.asyncio.api() is async_api
    assert kr8s.api() is api
    assert async_api is not api

    assert await async_api.version() == api.version()
    assert await async_api.whoami() == api.whoami()

    assert (await anext(async_api.get("ns")))._asyncio is True
    assert next(api.get("ns"))._asyncio is False


async def test_bad_api_version() -> None:
    api = await kr8s.asyncio.api()
    with pytest.raises(ValueError):
        async with api.call_api("GET", version="foo"):
            pass  # pragma: no cover


@pytest.mark.parametrize("namespace", [kr8s.ALL, "kube-system"])
async def test_get_pods(namespace) -> None:
    pods = [po async for po in kr8s.asyncio.get("pods", namespace=namespace)]
    assert isinstance(pods, list)
    assert len(pods) > 0
    assert isinstance(pods[0], Pod)


async def test_get_by_name_does_not_need_list(
    example_pod_spec, ns, get_only_serviceaccount
) -> None:
    """Getting one Pod by name must ask for `get`, not `list`.

    The `pytest-get-only` service account is granted `get` on Pods and
    nothing else. See https://github.com/kr8s-org/kr8s/issues/680.
    """
    pod = await Pod(example_pod_spec)
    await pod.create()
    # `kubeconfig` has to be pointed away, or the KUBECONFIG the test session
    # exports wins and the api is the cluster admin. See `test_service_account`.
    api = await kr8s.asyncio.api(
        serviceaccount=get_only_serviceaccount, kubeconfig="/no/file/here"
    )

    # Through the class and through the api, by name.
    assert (await Pod.get(pod.name, namespace=ns, api=api)).name == pod.name
    [found] = [p async for p in api.get("pods", pod.name, namespace=ns)]
    assert found.name == pod.name

    # Listing the collection is what this account may not do, and still may not.
    with pytest.raises(kr8s.ServerError):
        [p async for p in api.get("pods", namespace=ns)]

    # A name that matches nothing is still an empty iterator, not a 404.
    assert [p async for p in api.get("pods", "does-not-exist", namespace=ns)] == []


async def test_get_custom_resouces(example_crd) -> None:
    async for shirt in kr8s.asyncio.get(example_crd.name):
        assert shirt


async def test_get_pods_as_table() -> None:
    api = await kr8s.asyncio.api()
    async for pods in api.get("pods", namespace="kube-system", as_object=Table):
        assert isinstance(pods, Table)
        assert len(pods.rows) > 0
        assert not await pods.exists()  # Cannot exist in the Kubernetes API


async def test_watch_pods(example_pod_spec, ns) -> None:
    pod = await Pod(example_pod_spec)
    await pod.create()
    while not await pod.ready():
        await anyio.sleep(0.1)
    async for event, obj in kr8s.asyncio.watch("pods", namespace=ns):
        assert event in ["ADDED", "MODIFIED", "DELETED"]
        assert isinstance(obj, Pod)
        if obj.name == pod.name:
            if event == "ADDED":
                await obj.patch({"metadata": {"labels": {"test": "test"}}})
            elif event == "MODIFIED" and "test" in obj.labels and await obj.exists():
                await obj.delete()
                while await obj.exists():
                    await anyio.sleep(0.1)
            elif event == "DELETED":
                break


async def test_get_deployments() -> None:
    api = await kr8s.asyncio.api()
    deployments = [dply async for dply in api.get("deployments")]
    assert isinstance(deployments, list)


async def test_get_class() -> None:
    api = await kr8s.asyncio.api()
    pods = [pod async for pod in api.get(Pod, namespace=kr8s.ALL)]
    assert isinstance(pods, list)
    assert len(pods) > 0
    assert isinstance(pods[0], Pod)


async def test_api_versions() -> None:
    api = await kr8s.asyncio.api()
    versions = [version async for version in api.api_versions()]
    assert "apps/v1" in versions


def test_api_versions_sync():
    api = kr8s.api()
    versions = [version for version in api.api_versions()]
    assert "apps/v1" in versions


async def test_api_resources() -> None:
    resources = await kr8s.asyncio.api_resources()

    names = [r["name"] for r in resources]
    assert "nodes" in names
    assert "pods" in names
    assert "services" in names
    assert "namespaces" in names

    [pods] = [r for r in resources if r["name"] == "pods"]
    assert pods["namespaced"]
    assert pods["kind"] == "Pod"
    assert pods["version"] == "v1"
    assert "get" in pods["verbs"]

    [deployment] = [d for d in resources if d["name"] == "deployments"]
    assert deployment["namespaced"]
    assert deployment["kind"] == "Deployment"
    assert deployment["version"] == "apps/v1"
    assert "get" in deployment["verbs"]
    assert "deploy" in deployment["shortNames"]


async def test_ns(ns) -> None:
    api = await kr8s.asyncio.api(namespace=ns)
    assert ns == api.namespace

    api.namespace = "foo"
    assert api.namespace == "foo"


async def test_async_get_returns_async_objects() -> None:
    pods = [po async for po in kr8s.asyncio.get("pods", namespace=kr8s.ALL)]
    assert pods[0]._asyncio is True


def test_sync_get_returns_sync_objects() -> None:
    pods = list(kr8s.get("pods", namespace=kr8s.ALL))
    assert pods[0]._asyncio is False
    pods[0].refresh()


def test_sync_api_returns_sync_objects():
    api = kr8s.api()
    pods = api.get("pods", namespace=kr8s.ALL)
    pod = next(pods)
    assert pod._asyncio is False
    pod.refresh()


async def test_api_names(example_pod_spec: dict, ns: str) -> None:
    pod = await Pod(example_pod_spec)
    await pod.create()
    assert pod in [pod async for pod in kr8s.asyncio.get("pods", namespace=ns)]
    assert pod in [pod async for pod in kr8s.asyncio.get("pods/v1", namespace=ns)]
    assert pod in [pod async for pod in kr8s.asyncio.get("Pod", namespace=ns)]
    assert pod in [pod async for pod in kr8s.asyncio.get("pod", namespace=ns)]
    assert pod in [pod async for pod in kr8s.asyncio.get("po", namespace=ns)]
    await pod.delete()

    [role async for role in kr8s.asyncio.get("roles", namespace=ns)]
    [
        role
        async for role in kr8s.asyncio.get(
            "roles.rbac.authorization.k8s.io", namespace=ns
        )
    ]
    [
        role
        async for role in kr8s.asyncio.get(
            "roles.v1.rbac.authorization.k8s.io", namespace=ns
        )
    ]
    [
        role
        async for role in kr8s.asyncio.get(
            "roles.rbac.authorization.k8s.io/v1", namespace=ns
        )
    ]


async def test_whoami() -> None:
    api = await kr8s.asyncio.api()
    assert await kr8s.asyncio.whoami() == await api.whoami()


async def test_whoami_sync() -> None:
    api = kr8s.api()
    assert kr8s.whoami() == api.whoami()


async def test_api_resources_cache(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO")
    api = await kr8s.asyncio.api()
    await api.api_resources()
    assert caplog.text.count('/apis/ "HTTP/1.1 200 OK"') == 1
    await api.api_resources()
    assert caplog.text.count('/apis/ "HTTP/1.1 200 OK"') == 1


async def test_api_timeout() -> None:
    from httpx import Timeout

    api = await kr8s.asyncio.api()
    api.timeout = 10
    await api.version()
    assert api._session
    assert api._session.timeout.read == 10
    api.timeout = 20
    await api.version()
    assert api._session.timeout.read == 20
    api.timeout = Timeout(30)
    await api.version()
    assert api._session.timeout.read == 30

    api.timeout = 0.00001
    with pytest.raises(APITimeoutError):
        await api.version()


async def test_lookup_kind():
    api = await kr8s.asyncio.api()

    assert await api.lookup_kind("no") == ("Node/v1", "nodes", False)
    assert await api.lookup_kind("nodes") == ("Node/v1", "nodes", False)
    assert await api.lookup_kind("po") == ("Pod/v1", "pods", True)
    assert await api.lookup_kind("pods/v1") == ("Pod/v1", "pods", True)
    assert await api.lookup_kind("CSIStorageCapacity") == (
        "CSIStorageCapacity.storage.k8s.io/v1",
        "csistoragecapacities",
        True,
    )
    assert await api.lookup_kind("role") == (
        "Role.rbac.authorization.k8s.io/v1",
        "roles",
        True,
    )
    assert await api.lookup_kind("roles") == (
        "Role.rbac.authorization.k8s.io/v1",
        "roles",
        True,
    )
    assert await api.lookup_kind("roles.v1.rbac.authorization.k8s.io") == (
        "Role.rbac.authorization.k8s.io/v1",
        "roles",
        True,
    )
    assert await api.lookup_kind("roles.rbac.authorization.k8s.io") == (
        "Role.rbac.authorization.k8s.io/v1",
        "roles",
        True,
    )


async def test_lookup_kind_with_a_hyphenated_singular(example_crd_spec):
    """A CRD whose singular is not the lowercased Kind.

    `parse_kind` lowercases, so `NetworkAttachmentDefinition` matches none
    of the plural, the singular or the short names. Only a case-folded
    compare against the Kind itself finds it. Hyphenated singulars are legal
    and common in the CNI ecosystem.
    """
    spec = copy.deepcopy(example_crd_spec)
    spec["metadata"]["name"] = "network-attachment-definitions.stable.example.com"
    spec["spec"]["names"] = {
        "plural": "network-attachment-definitions",
        "singular": "network-attachment-definition",
        "kind": "NetworkAttachmentDefinition",
    }

    async with create_delete_crd(spec):
        api = await kr8s.asyncio.api()
        assert await api.lookup_kind("NetworkAttachmentDefinition") == (
            "NetworkAttachmentDefinition.stable.example.com/v1",
            "network-attachment-definitions",
            True,
        )


async def test_unknown_kind_keeps_its_case(example_crd, ensure_gc):
    """A kind kr8s has no class for still reports the Kind the server serves.

    `ensure_gc` because `new_class` registers the class it builds as an
    `APIObject` subclass, and `get_class` walks those. A Shirt left behind
    here answers a later test's lookup.
    """
    api = await kr8s.asyncio.api()

    # Uncached: a CRD created after the cache was filled is not in it.
    kind, plural, namespaced = await api.async_lookup_kind("shirt", skip_cache=True)
    assert (kind, plural, namespaced) == (
        "Shirt.stable.example.com/v1",
        "shirts",
        True,
    )

    # The class `async_get_kind` builds for a kind with no builtin class.
    shirt = new_class(kind, namespaced=namespaced, plural=plural)
    assert shirt.kind == "Shirt"
    assert shirt.version == "stable.example.com/v1"
    del shirt


async def test_nonexisting_resource_type():
    api = await kr8s.asyncio.api()

    with pytest.raises(ValueError):
        async for _ in api.get("foo.bar.baz/v1"):
            pass


async def test_get_an_unknown_kind_when_discovery_fails():
    """A discovery error leaves `lookup_kind` without a plural.

    `async_get_kind` warns and carries on, so what follows has to work with
    what it has. It used to read a `plural` that the failed lookup never
    assigned, and raise `UnboundLocalError` from inside the warning path.
    That is not a ServerError, so a caller guarding against one did not
    catch it either.

    The call still cannot succeed -- without discovery there is no API
    group to address -- but it now reaches kr8s' own error for that, which
    says so.

    The kind does not have to exist. The patched lookup raises before
    anything consults the cluster, so the name is only a string.
    """
    api = await kr8s.asyncio.api()
    boom = ServerError("the server is currently unable to handle the request")

    with patch.object(api, "async_lookup_kind", side_effect=boom):
        with pytest.warns(UserWarning, match="unable to handle"):
            with pytest.raises(ValueError, match="Unknown API version"):
                async for _ in api.async_get(
                    "shirts.stable.example.com", namespace=kr8s.ALL
                ):
                    pass


@pytest.mark.parametrize(
    "kind",
    [
        "csr",
        "certificatesigningrequest",
        "certificatesigningrequests",
        "certificatesigningrequest.certificates.k8s.io",
        "certificatesigningrequests.certificates.k8s.io",
        "certificatesigningrequest.v1.certificates.k8s.io",
        "certificatesigningrequests.v1.certificates.k8s.io",
        "certificatesigningrequest.certificates.k8s.io/v1",
        "certificatesigningrequests.certificates.k8s.io/v1",
    ],
)
async def test_dynamic_classes(kind, ensure_gc):
    from kr8s.asyncio.objects import get_class

    api = await kr8s.asyncio.api()

    with pytest.raises(KeyError):
        get_class("certificatesigningrequest", "certificates.k8s.io/v1")

    with pytest.raises(KeyError):
        async for _ in api.get(kind, allow_unknown_type=False):
            pass

    async for _ in api.get(kind):
        pass


@pytest.mark.parametrize(
    "kind",
    [
        "ingress.networking.k8s.io",
        "networkpolicies.networking.k8s.io",
        "csistoragecapacities.storage.k8s.io",
        "CSIStorageCapacity",
    ],
)
async def test_get_dynamic_plurals(kind, ensure_gc):
    api = await kr8s.asyncio.api()
    assert isinstance([resource async for resource in api.get(kind)], list)


async def test_two_pods(ns, pause_image):
    gen_kwargs = {
        "generate_name": "example-",
        "image": pause_image,
        "namespace": ns,
    }
    pods = [await Pod.gen(**gen_kwargs), await Pod.gen(**gen_kwargs)]

    async with anyio.create_task_group() as tg:
        for pod in pods:
            tg.start_soon(pod.create)

    async with anyio.create_task_group() as tg:
        for pod in pods:
            tg.start_soon(pod.wait, "condition=Ready")

    pods_api = [
        pod
        async for pod in kr8s.asyncio.get(
            "Pod", pods[0].name, pods[1].name, namespace=ns
        )
    ]
    assert len(pods_api) == 2

    async with anyio.create_task_group() as tg:
        for pod in pods:
            tg.start_soon(pod.delete)


async def test_create(example_pod_spec, example_service_spec):
    pod = await Pod(example_pod_spec)
    service = await Service(example_service_spec)
    resources = [pod, service]
    await kr8s.asyncio.create(resources)
    assert await pod.exists(), "Pod should exist after creation"
    assert await service.exists(), "Service should exist after creation"
    await pod.delete()
    await service.delete()


async def test_create_uses_the_api_it_is_given(example_pod_spec, k8s_cluster):
    """`kr8s.create(resources, api=...)` sends through the api it is given.

    It used to send through `resource.api` instead, so the argument decided
    nothing. With two clusters that is a silent write to the wrong one.
    """
    # Bind the pod first: `api()` with no arguments returns whatever is
    # already cached, so creating `other` first would bind the pod to it and
    # the test would prove nothing.
    pod = await Pod(example_pod_spec)
    kubeconfig = yaml.safe_load(k8s_cluster.kubeconfig_path.read_text())
    other = await kr8s.asyncio.api(context=kubeconfig["current-context"])
    assert pod.api is not other, "the pod must be bound to a different api"

    calls = []
    real = other.call_api

    def recording(*args, **kwargs):
        calls.append(kwargs.get("method", args[0] if args else None))
        return real(*args, **kwargs)

    other.call_api = recording
    try:
        await kr8s.asyncio.create([pod], api=other)
    finally:
        other.call_api = real

    assert "POST" in calls, f"create() did not use the api it was given; calls={calls}"
    assert await pod.exists()
    await pod.delete()


async def test_create_without_an_api_keeps_the_objects_binding(
    example_pod_spec, k8s_cluster
):
    """With no `api` argument, each resource is sent through the api it is
    bound to, so `create([obj])` and `obj.create()` reach the same cluster.

    Resolving a default here and sending everything through that would move
    an object that was explicitly given an api of its own.
    """
    # `api()` with no arguments returns the first cached instance, so take
    # that one first: `bound` must not be the api the helper would resolve,
    # or forcing the resolved one would look identical to honouring the
    # binding and this would pass either way.
    default = await kr8s.asyncio.api()
    kubeconfig = yaml.safe_load(k8s_cluster.kubeconfig_path.read_text())
    bound = await kr8s.asyncio.api(context=kubeconfig["current-context"])
    assert bound is not default

    pod = await Pod(example_pod_spec, api=bound)
    assert pod.api is bound

    calls = []
    real = bound.call_api

    def recording(*args, **kwargs):
        calls.append(kwargs.get("method", args[0] if args else None))
        return real(*args, **kwargs)

    bound.call_api = recording
    try:
        await kr8s.asyncio.create([pod])
    finally:
        bound.call_api = real

    assert "POST" in calls, f"create() did not use the pod's own api; calls={calls}"
    await pod.delete()


def test_create_sync(example_pod_spec, example_service_spec):
    pod = SyncPod(example_pod_spec)
    service = SyncService(example_service_spec)
    assert pod._asyncio is False
    assert service._asyncio is False
    resources = [pod, service]
    kr8s.create(resources)
    assert pod.exists(), "Pod should exist after creation"
    assert service.exists(), "Service should exist after creation"
    pod.delete()
    service.delete()


async def test_create_with_apply(example_pod_spec, example_service_spec):
    pod = await Pod(example_pod_spec)
    service = await Service(example_service_spec)
    resources = [pod, service]
    await kr8s.asyncio.apply(resources)
    assert await pod.exists(), "Pod should exist after creation"
    assert await service.exists(), "Service should exist after creation"
    await pod.delete()
    await service.delete()


async def test_update_with_apply(example_pod_spec, example_service_spec):
    pod = await Pod(example_pod_spec)
    service = await Service(example_service_spec)
    resources = [pod, service]
    await kr8s.asyncio.create(resources)
    pod.labels["foo"] = "bar"
    await kr8s.asyncio.apply([pod])
    assert pod.labels["foo"] == "bar", "Apply should send updated resource"
    updated_pod = await Pod.get(pod.name, namespace=pod.namespace)
    assert (
        updated_pod.labels["foo"] == "bar"
    ), "Pod we got by re-fetching should have updated labels"
    await pod.delete()


async def test_apply_update_with_ssa(example_pod_spec, example_service_spec):
    pod = await Pod(example_pod_spec)
    service = await Service(example_service_spec)
    resources = [pod, service]
    await kr8s.asyncio.apply(resources, server_side=True)

    pod.labels["foo"] = "bar"
    await pod.apply(server_side=True)
    assert pod.labels["foo"] == "bar", "SSA update should send updated resource"
    assert await pod.exists(), "Pod should exist after creation"
    assert pod.labels["foo"] == "bar", "SSA update should send updated resource"


async def test_apply_update_with_ssa_force(example_pod_spec, example_service_spec):
    """
    SSA has semantics about modifying fields owned by other managers.

    We would need to use the force option to override this.
    """
    pod = await Pod(example_pod_spec)
    pod.labels["my_field"] = "other-manager"
    service = await Service(example_service_spec)
    resources = [pod, service]

    other_api = await kr8s.asyncio.api(field_manager="other-manager")
    pod.api = other_api  # api param in helpers is ignored
    await kr8s.asyncio.apply(resources, server_side=True)
    assert await pod.exists(), "Pod should exist after creation"

    api = await kr8s.asyncio.api(field_manager="kr8s")
    pod.api = api  # api param in helpers is ignored
    with pytest.RaisesGroup(ServerError):
        pod.labels["my_field"] = "changed"
        await kr8s.asyncio.apply([pod], server_side=True)

    await kr8s.asyncio.apply([pod], server_side=True, force_conflicts=True)
    assert await pod.exists(), "Pod should exist after creation"
    assert (
        pod.labels["my_field"] == "changed"
    ), "SSA update should send updated resource"


async def test_apply_creates_if_not_exists(example_pod_spec):
    pod = await Pod(example_pod_spec)
    assert not await pod.exists()
    await pod.apply()
    assert await pod.exists(), "Pod should exist after creation"


async def test_apply_uses_the_api_it_is_given(example_pod_spec):
    """`Api.async_apply` delegates to the resource, so without passing itself
    the request goes through `resource.api` and the `api` argument decides
    nothing. Same defect as `create` had, same consequence: with two clusters
    the object lands in the wrong one."""
    pod = await Pod(example_pod_spec)
    other = await kr8s.asyncio.api(field_manager="other-manager")
    assert pod.api is not other

    await kr8s.asyncio.apply([pod], api=other, server_side=True)

    managers = {e["manager"] for e in pod.raw["metadata"]["managedFields"]}
    assert (
        "other-manager" in managers
    ), f"apply used the pod's own api, not the one passed in; managers={managers}"
    await pod.delete()


async def test_apply_without_an_api_keeps_the_objects_binding(example_pod_spec):
    """With no argument each resource keeps the api it is bound to, so
    `apply([obj])` and `obj.apply()` reach the same cluster."""
    default = await kr8s.asyncio.api()
    bound = await kr8s.asyncio.api(field_manager="bound-to-the-object")
    assert bound is not default

    pod = await Pod(example_pod_spec, api=bound)
    await kr8s.asyncio.apply([pod], server_side=True)

    managers = {e["manager"] for e in pod.raw["metadata"]["managedFields"]}
    assert (
        "bound-to-the-object" in managers
    ), f"apply ignored the api the pod is bound to; managers={managers}"
    await pod.delete()


async def test_apply_takes_a_field_manager_per_call(example_pod_spec):
    """The client-wide binding cannot express a caller that applies objects
    under several managers in one run, so the argument overrides it."""
    api = await kr8s.asyncio.api(field_manager="bound-to-the-client")
    pod = await Pod(example_pod_spec, api=api)

    await pod.apply(server_side=True, field_manager="per-call")

    managers = {e["manager"] for e in pod.raw["metadata"]["managedFields"]}
    assert "per-call" in managers
    assert "bound-to-the-client" not in managers
    await pod.delete()


async def test_apply_returns_the_merged_object(example_pod_spec):
    """A dry run stores nothing, so what it returns is the only way to see
    the result. `ekn clusterdiff` is built on exactly this."""
    pod = await Pod(example_pod_spec)

    merged = await pod.apply(server_side=True, dry_run="server")

    assert merged["metadata"]["name"] == pod.name
    # Defaulted by the API server, so it proves the answer came from there
    # rather than being the object we sent.
    assert merged["spec"]["restartPolicy"]
    assert not await pod.exists()


async def test_a_failed_apply_leaves_managed_fields_alone(example_pod_spec):
    """The apply body is built from a copy, so a request that never succeeds
    does not leave the caller's object stripped of a field it had."""
    pod = await Pod(example_pod_spec)
    await pod.apply()
    assert pod.raw["metadata"]["managedFields"]

    pod["my_field"] = "value"
    with pytest.raises(ServerError):
        await pod.apply(validate="strict")

    assert pod.raw["metadata"][
        "managedFields"
    ], "a failed apply destroyed managedFields on the object"


@pytest.mark.parametrize("server_side", [False, True])
async def test_apply_twice_over_the_same_object(example_pod_spec, server_side):
    """An apply stores the API server's answer in ``raw``, so a second apply
    of the same object sends back whatever that answer carried.

    Two fields in it are refused. ``metadata.managedFields`` comes back with
    "must be nil", and ``metadata.resourceVersion`` makes the request an
    optimistic lock, so it answers 409 as soon as anything else has written
    to the object. Neither is a field the caller asked to send.

    ``async_apply`` drops the first and ``raw_template`` drops the second.
    This covers both, because nothing else applies the same object twice.
    """
    pod = await Pod(example_pod_spec)
    await pod.apply(server_side=server_side)
    assert "managedFields" in pod.raw["metadata"]
    assert "resourceVersion" in pod.raw["metadata"]

    # Move the object on through a second handle, so the resourceVersion this
    # one holds is stale -- what another writer does to it in the meantime.
    other = await Pod.get(pod.name, namespace=pod.namespace)
    await other.patch({"metadata": {"labels": {"bump": "1"}}})

    await pod.apply(server_side=server_side)
    assert await pod.exists(), "Pod should still exist after a second apply"


@pytest.mark.parametrize("dry_run", ["server", True])
async def test_apply_dry_run_does_not_persist(example_pod_spec, dry_run):
    pod = await Pod(example_pod_spec)
    await pod.apply(dry_run=dry_run)
    assert not await pod.exists(), "A dry run must not create the object"


async def test_apply_dry_run_leaves_raw_alone(example_pod_spec):
    """A dry run stores nothing, so keeping its answer would leave the object
    holding a resourceVersion and a uid that no stored object has."""
    pod = await Pod(example_pod_spec)
    await pod.apply()
    before = copy.deepcopy(pod.raw.to_dict())

    await pod.apply(dry_run="server")

    assert pod.raw.to_dict() == before


async def test_apply_dry_run_still_validates(example_pod_spec):
    """The point of a server dry run: admission and validation run, so this
    is refused without anything being stored."""
    pod = await Pod(example_pod_spec)
    pod["my_field"] = "value"
    with pytest.raises(ServerError):
        await pod.apply(dry_run="server", validate="strict")
    assert not await pod.exists()


@pytest.mark.parametrize("dry_run", ["none", False])
async def test_apply_without_dry_run_persists(example_pod_spec, dry_run):
    pod = await Pod(example_pod_spec)
    await pod.apply(dry_run=dry_run)
    assert await pod.exists()


async def test_apply_rejects_an_unknown_dry_run_strategy(example_pod_spec):
    """`kubectl` also takes `--dry-run=client`, which has nothing to print
    here. Accepting it as a no-op would make a caller who asked for a dry run
    get a real write, so it is refused instead."""
    pod = await Pod(example_pod_spec)
    with pytest.raises(ValueError, match="Invalid dry_run option"):
        await pod.apply(dry_run="client")
    assert not await pod.exists()


async def test_apply_validate_strict(example_pod_spec):
    pod = await Pod(example_pod_spec)
    pod["my_field"] = "value"
    with pytest.raises(ServerError):
        await pod.apply(validate="strict")


async def test_apply_validate_warn(example_pod_spec, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.WARNING)
    pod = await Pod(example_pod_spec)
    pod["my_field0"] = "value"
    pod["my_field1"] = "value"
    await pod.apply(validate="warn")
    assert any(
        r"unknown field \"my_field0\"" in record.msg for record in caplog.records
    )
    assert any(
        r"unknown field \"my_field1\"" in record.msg for record in caplog.records
    )


async def test_apply_validate_ignore(
    example_pod_spec, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.WARNING)
    pod = await Pod(example_pod_spec)
    pod["my_field0"] = "value"
    pod["my_field1"] = "value"
    await pod.apply(validate="ignore")
    assert all(r"unknown field" not in record.msg for record in caplog.records)


@pytest.mark.parametrize(
    "version",
    [
        "1.27.0",
        "v1.27.0",
        "1.27.0-eks-113cf36",
        "v1.27.0-eks-113cf36",
        f"{KUBERNETES_MAXIMUM_SUPPORTED_VERSION.major}.{KUBERNETES_MAXIMUM_SUPPORTED_VERSION.minor + 1}",
        "asdkjhaskdjhasd",
    ],
)
async def test_bad_kubernetes_version(version):
    api = await kr8s.asyncio.api()
    keep = api.async_version
    api.async_version = AsyncMock(return_value={"gitVersion": version})
    with pytest.warns(UserWarning, match=version):
        await api._check_version()
    api.async_version = keep


@pytest.mark.parametrize(
    "version",
    [
        str(KUBERNETES_MINIMUM_SUPPORTED_VERSION),
        str(KUBERNETES_MAXIMUM_SUPPORTED_VERSION),
        f"{KUBERNETES_MAXIMUM_SUPPORTED_VERSION.major}.{KUBERNETES_MAXIMUM_SUPPORTED_VERSION.minor}.15",
        f"{KUBERNETES_MINIMUM_SUPPORTED_VERSION}-eks-113cf36",
    ],
)
async def test_good_kubernetes_version(version):
    api = await kr8s.asyncio.api()
    keep = api.async_version
    api.async_version = AsyncMock(return_value={"gitVersion": version})
    with warnings.catch_warnings(record=True) as w:
        await api._check_version()
        assert w == []
    api.async_version = keep


async def test_crd_caching(example_crd_spec):
    api = await kr8s.asyncio.api()

    # Populate the cache
    [r async for r in api.get("pods")]

    # Register a new CRD
    async with create_delete_crd(example_crd_spec) as example_crd:
        # Try to get the new CRD (which isn't in the cache, so the cache should be bypassed)
        [r async for r in api.get(example_crd.name)]


async def test_get_raw_basic() -> None:
    """Test getting resources with raw=True returns dictionaries, not APIObject instances."""
    api = await kr8s.asyncio.api()
    pods = [pod async for pod in api.get("pods", namespace="kube-system", raw=True)]
    assert isinstance(pods, list)
    assert len(pods) > 0
    # Should be dictionaries, not Pod objects
    assert isinstance(pods[0], dict)
    assert "metadata" in pods[0]
    assert "name" in pods[0]["metadata"]


async def test_get_raw_false_default() -> None:
    """Test that default behavior (without raw parameter) returns APIObject instances."""
    api = await kr8s.asyncio.api()
    pods = [pod async for pod in api.get("pods", namespace="kube-system")]
    assert isinstance(pods, list)
    assert len(pods) > 0
    # Should be Pod objects, not dictionaries
    assert isinstance(pods[0], Pod)
    assert not isinstance(pods[0], dict)


async def test_get_raw_with_as_object() -> None:
    """Test that when both as_object and raw=True are specified, yields the raw dictionary."""
    api = await kr8s.asyncio.api()
    async for result in api.get(
        "pods", namespace="kube-system", as_object=Table, raw=True
    ):
        # Should be a dictionary, not a Table object
        assert isinstance(result, dict)
        assert "kind" in result
        assert result["kind"] == "Table"
        # When as_object is specified, the API returns a single object (Table format)
        break


async def test_get_raw_with_label_selector() -> None:
    """Test that label selectors work with raw=True."""
    selector = {"component": "kube-apiserver"}
    pods = [
        pod
        async for pod in kr8s.asyncio.get(
            "pods", namespace="kube-system", label_selector=selector, raw=True
        )
    ]
    # Should get dictionaries
    for pod in pods:
        assert isinstance(pod, dict)
        assert "metadata" in pod
        if "labels" in pod["metadata"]:
            # If labels exist, verify the selector matches
            assert pod["metadata"]["labels"].get("component") == "kube-apiserver"


async def test_get_raw_with_field_selector() -> None:
    """Test that field selectors work with raw=True."""
    pods = [
        pod
        async for pod in kr8s.asyncio.get(
            "pods",
            namespace="kube-system",
            field_selector="status.phase=Running",
            raw=True,
        )
    ]
    # Should get dictionaries
    assert len(pods) > 0
    for pod in pods:
        assert isinstance(pod, dict)
        assert pod["status"]["phase"] == "Running"


def test_get_raw_sync() -> None:
    """Test the sync version (kr8s.get()) with raw=True."""
    pods = list(kr8s.get("pods", namespace="kube-system", raw=True))
    assert isinstance(pods, list)
    assert len(pods) > 0
    # Should be dictionaries, not Pod objects
    assert isinstance(pods[0], dict)
    assert "metadata" in pods[0]
    assert not isinstance(pods[0], SyncPod)


async def test_list_raw() -> None:
    """Test the APIObject.list() classmethod with raw=True returns dictionaries."""
    pods = [pod async for pod in Pod.list(namespace="kube-system", raw=True)]
    assert isinstance(pods, list)
    assert len(pods) > 0
    # Should be dictionaries, not Pod objects
    assert isinstance(pods[0], dict)
    assert "metadata" in pods[0]
    assert not isinstance(pods[0], Pod)
