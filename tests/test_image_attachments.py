"""Turning what the browser posts into bytes a provider can accept.

Both input forms resolve here, before any provider client exists, so that every provider
behaves identically -- native Ollama has no URL form at all. The URL path is exercised
through httpx.MockTransport and an injected resolver; nothing leaves the machine and no
test depends on DNS.
"""
import base64
from ipaddress import ip_address

import httpx
import pytest
from fastapi import HTTPException

from corbelity.workbench import app as workbench
from corbelity.workbench.app import ImageAttachment

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")

PUBLIC_IP = "93.184.216.34"


def resolver(mapping=None, default=PUBLIC_IP):
    """A stand-in for DNS. Unlisted hosts resolve to an ordinary public address."""
    table = mapping or {}
    def resolve(host):
        return [ip_address(a) for a in table.get(host, [default])]
    return resolve


def mock_client(handler):
    """An httpx client whose transport answers from `handler` instead of the network."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def serving(content, *, status=200, content_type="image/png"):
    def handler(request):
        return httpx.Response(status, content=content, headers={"content-type": content_type})
    return mock_client(handler)


def fetch(attachments, client=None, resolve=None):
    """normalize_attachments with the test doubles wired in by default."""
    return workbench.normalize_attachments(
        attachments, client=client, resolve=resolve or resolver())


# ------------------------------ data URLs ----------------------------------- #
def test_a_data_url_becomes_bytes():
    [image] = fetch([ImageAttachment(data_url=PNG_DATA_URL, name="chart.png")])
    assert image.data == PNG
    assert image.mime_type == "image/png"
    assert image.name == "chart.png"


def test_no_attachments_resolves_to_an_empty_list():
    assert fetch([]) == []


def test_malformed_base64_is_a_400_not_a_500():
    """A bad paste is the caller's problem to fix, not a server fault."""
    with pytest.raises(HTTPException) as err:
        fetch([ImageAttachment(data_url="data:image/png;base64,%%%%")])
    assert err.value.status_code == 400


def test_a_data_url_that_is_not_an_image_is_rejected():
    with pytest.raises(HTTPException) as err:
        fetch([ImageAttachment(data_url="data:text/plain;base64,aGVsbG8=")])
    assert err.value.status_code == 400


def test_whitespace_inside_a_data_url_payload_is_tolerated():
    """A hand-pasted URL often carries newlines; a browser's never does."""
    encoded = base64.b64encode(PNG).decode("ascii")
    wrapped = "data:image/png;base64," + "\n".join(
        encoded[i:i + 16] for i in range(0, len(encoded), 16))
    [image] = fetch([ImageAttachment(data_url=wrapped)])
    assert image.data == PNG


# -------------------------------- URLs -------------------------------------- #
def test_a_url_is_fetched_into_bytes():
    [image] = fetch([ImageAttachment(url="https://example.com/a/chart.png")],
                    client=serving(PNG))
    assert image.data == PNG
    assert image.mime_type == "image/png"


def test_the_filename_is_derived_from_the_url_path():
    [image] = fetch([ImageAttachment(url="https://example.com/a/chart.png")],
                    client=serving(PNG))
    assert image.name == "chart.png"


def test_an_explicit_name_beats_the_url_path():
    [image] = fetch([ImageAttachment(url="https://example.com/a/chart.png", name="mine.png")],
                    client=serving(PNG))
    assert image.name == "mine.png"


def test_a_generic_content_type_falls_back_to_magic_bytes():
    """Servers routinely return application/octet-stream; the provider needs the truth."""
    [image] = fetch([ImageAttachment(url="https://example.com/x")],
                    client=serving(PNG, content_type="application/octet-stream"))
    assert image.mime_type == "image/png"


def test_a_content_type_with_parameters_is_stripped():
    [image] = fetch([ImageAttachment(url="https://example.com/x")],
                    client=serving(PNG, content_type="image/png; charset=binary"))
    assert image.mime_type == "image/png"


def test_a_non_2xx_response_is_a_400_quoting_the_status():
    with pytest.raises(HTTPException) as err:
        fetch([ImageAttachment(url="https://example.com/missing")],
              client=serving(b"nope", status=404))
    assert err.value.status_code == 400
    assert "404" in err.value.detail


def test_a_transport_failure_is_a_400_not_a_500():
    def boom(request):
        raise httpx.ConnectError("no route to host", request=request)

    with pytest.raises(HTTPException) as err:
        fetch([ImageAttachment(url="https://example.com/x")], client=mock_client(boom))
    assert err.value.status_code == 400


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x.png", "/local/x.png"])
def test_only_http_urls_are_fetched(url):
    """The scheme check runs before anything is resolved or connected, so this needs no
    client and no resolver."""
    with pytest.raises(HTTPException) as err:
        workbench.normalize_attachments([ImageAttachment(url=url)])
    assert err.value.status_code == 400


def test_an_oversized_body_is_refused_mid_stream():
    """Content-Length is a claim; the cap is enforced against what actually arrives."""
    oversized = b"\x89PNG\r\n\x1a\n" + b"\x00" * (workbench.MAX_IMAGE_BYTES + 1)
    with pytest.raises(HTTPException) as err:
        fetch([ImageAttachment(url="https://example.com/big.png")], client=serving(oversized))
    assert err.value.status_code == 400


# --------------------------- destination policy ----------------------------- #
# The fetch runs on the server, so a URL is an instruction to make THIS machine issue a
# request. On a cloud VM or any host with reachable internal services, an unchecked
# destination is a server-side request forgery primitive.
@pytest.mark.parametrize(
    "address, reason",
    [
        ("127.0.0.1", "loopback"),
        ("::1", "loopback"),
        ("10.0.0.5", "private"),
        ("172.16.4.1", "private"),
        ("192.168.1.1", "private"),
        ("169.254.169.254", "link-local"),   # cloud instance metadata
        ("fd00::1", "private"),              # IPv6 unique-local
        ("fe80::1", "link-local"),
        ("0.0.0.0", "unspecified"),
        ("224.0.0.1", "multicast"),
        ("240.0.0.1", "reserved"),
    ],
)
def test_non_public_addresses_are_named_and_blocked(address, reason):
    assert workbench.address_block_reason(ip_address(address)) == reason


@pytest.mark.parametrize("address", [PUBLIC_IP, "1.1.1.1", "2606:4700::1111"])
def test_public_addresses_are_allowed(address):
    assert workbench.address_block_reason(ip_address(address)) is None


def test_a_literal_internal_address_is_refused():
    with pytest.raises(HTTPException) as err:
        fetch([ImageAttachment(url="http://169.254.169.254/latest/meta-data/")],
              client=serving(PNG), resolve=resolver({"169.254.169.254": ["169.254.169.254"]}))
    assert err.value.status_code == 400
    assert "link-local" in err.value.detail


def test_a_hostname_resolving_inward_is_refused():
    """The name looks ordinary; only the resolved address gives it away."""
    with pytest.raises(HTTPException) as err:
        fetch([ImageAttachment(url="https://metadata.example.com/x.png")],
              client=serving(PNG), resolve=resolver({"metadata.example.com": ["10.0.0.5"]}))
    assert "private" in err.value.detail


def test_an_ipv4_mapped_ipv6_loopback_is_refused():
    """::ffff:127.0.0.1 is loopback in an IPv6 costume, and has to be unmapped before it
    is judged or the v4 checks never run."""
    with pytest.raises(HTTPException) as err:
        fetch([ImageAttachment(url="https://sneaky.example.com/x.png")],
              client=serving(PNG),
              resolve=resolver({"sneaky.example.com": ["::ffff:127.0.0.1"]}))
    assert "loopback" in err.value.detail


def test_a_host_with_one_bad_address_is_refused_entirely():
    """A name that round-robins between a public and an internal address must not come
    down to which answer arrived first."""
    with pytest.raises(HTTPException) as err:
        fetch([ImageAttachment(url="https://mixed.example.com/x.png")],
              client=serving(PNG),
              resolve=resolver({"mixed.example.com": [PUBLIC_IP, "127.0.0.1"]}))
    assert "loopback" in err.value.detail


def test_a_host_that_does_not_resolve_is_a_400():
    with pytest.raises(HTTPException) as err:
        fetch([ImageAttachment(url="https://nowhere.example.com/x.png")],
              client=serving(PNG), resolve=resolver({"nowhere.example.com": []}))
    assert err.value.status_code == 400


# ------------------------------- redirects ---------------------------------- #
def redirecting(hops, final=PNG):
    """Serves a redirect chain. `hops` maps a full request URL to its Location.

    Keyed on the whole URL rather than the host, because a relative redirect keeps the
    host: matching on host alone would send every hop straight back to the same redirect
    and never terminate."""
    def handler(request):
        target = hops.get(str(request.url))
        if target:
            return httpx.Response(302, headers={"location": target})
        return httpx.Response(200, content=final, headers={"content-type": "image/png"})
    return mock_client(handler)


def test_a_redirect_is_followed():
    [image] = fetch(
        [ImageAttachment(url="https://example.com/x.png")],
        client=redirecting({"https://example.com/x.png": "https://cdn.example.net/real.png"}))
    assert image.data == PNG


def test_the_final_url_names_the_file():
    [image] = fetch(
        [ImageAttachment(url="https://example.com/x.png")],
        client=redirecting({"https://example.com/x.png": "https://cdn.example.net/real.png"}))
    assert image.name == "real.png"


def test_a_redirect_that_turns_inward_is_refused():
    """The whole reason redirects are followed by hand: a public URL redirecting to an
    internal one defeats a check that only ran on the original."""
    with pytest.raises(HTTPException) as err:
        fetch([ImageAttachment(url="https://example.com/x.png")],
              client=redirecting({
                  "https://example.com/x.png": "http://169.254.169.254/latest/meta-data/"}),
              resolve=resolver({"169.254.169.254": ["169.254.169.254"]}))
    assert err.value.status_code == 400
    assert "link-local" in err.value.detail


def test_a_redirect_to_a_non_http_scheme_is_refused():
    with pytest.raises(HTTPException) as err:
        fetch([ImageAttachment(url="https://example.com/x.png")],
              client=redirecting({"https://example.com/x.png": "file:///etc/passwd"}))
    assert err.value.status_code == 400


def test_a_relative_redirect_is_resolved_against_the_current_url():
    """A Location of "/b/real.png" has to be joined onto the URL that returned it."""
    [image] = fetch([ImageAttachment(url="https://example.com/a/x.png")],
                    client=redirecting({"https://example.com/a/x.png": "/b/real.png"}))
    assert image.name == "real.png"


def test_an_endless_redirect_chain_is_cut_off():
    with pytest.raises(HTTPException) as err:
        fetch([ImageAttachment(url="https://example.com/x.png")],
              client=redirecting({"https://example.com/x.png": "https://example.com/x.png"}))
    assert err.value.status_code == 400
    assert "redirects" in err.value.detail


def test_a_redirect_without_a_location_is_a_400():
    def handler(request):
        return httpx.Response(302)

    with pytest.raises(HTTPException) as err:
        fetch([ImageAttachment(url="https://example.com/x.png")], client=mock_client(handler))
    assert err.value.status_code == 400


# ------------------------------- input form --------------------------------- #
def test_an_attachment_with_neither_form_is_rejected_by_index():
    with pytest.raises(HTTPException) as err:
        fetch([ImageAttachment(data_url=PNG_DATA_URL), ImageAttachment(name="orphan.png")])
    assert err.value.status_code == 400
    assert "Image 1" in err.value.detail


def test_an_attachment_with_both_forms_is_rejected():
    with pytest.raises(HTTPException) as err:
        fetch([ImageAttachment(data_url=PNG_DATA_URL, url="https://example.com/x.png")])
    assert err.value.status_code == 400


# --------------------------------- caps ------------------------------------- #
def test_too_many_images_are_refused():
    many = [ImageAttachment(data_url=PNG_DATA_URL)] * (workbench.MAX_INPUT_IMAGES + 1)
    with pytest.raises(HTTPException) as err:
        fetch(many)
    assert err.value.status_code == 400


def test_exactly_the_limit_is_allowed():
    many = [ImageAttachment(data_url=PNG_DATA_URL)] * workbench.MAX_INPUT_IMAGES
    assert len(fetch(many)) == workbench.MAX_INPUT_IMAGES


def test_one_oversized_image_is_refused():
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (workbench.MAX_IMAGE_BYTES + 1)
    url = "data:image/png;base64," + base64.b64encode(big).decode("ascii")
    with pytest.raises(HTTPException) as err:
        fetch([ImageAttachment(data_url=url)])
    assert err.value.status_code == 400


def test_images_that_are_individually_fine_can_still_bust_the_total(monkeypatch):
    """Four images each just under the per-image cap is a request no provider accepts."""
    monkeypatch.setattr(workbench, "MAX_TOTAL_IMAGE_BYTES", 100)
    body = b"\x89PNG\r\n\x1a\n" + b"\x00" * 60
    url = "data:image/png;base64," + base64.b64encode(body).decode("ascii")
    with pytest.raises(HTTPException) as err:
        fetch([ImageAttachment(data_url=url), ImageAttachment(data_url=url)])
    assert err.value.status_code == 400
