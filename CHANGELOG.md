# suitenumerique\.st Release Notes

**Topics**

- <a href="#v0-2-2">v0\.2\.2</a>
    - <a href="#minor-changes">Minor Changes</a>
    - <a href="#bugfixes">Bugfixes</a>
- <a href="#v0-2-1">v0\.2\.1</a>
    - <a href="#minor-changes-1">Minor Changes</a>
- <a href="#v0-2-0">v0\.2\.0</a>
    - <a href="#release-summary">Release Summary</a>
    - <a href="#minor-changes-2">Minor Changes</a>
    - <a href="#bugfixes-1">Bugfixes</a>
- <a href="#v0-1-1">v0\.1\.1</a>
    - <a href="#minor-changes-3">Minor Changes</a>
    - <a href="#bugfixes-2">Bugfixes</a>
- <a href="#v0-1-0">v0\.1\.0</a>
    - <a href="#release-summary-1">Release Summary</a>
    - <a href="#major-changes">Major Changes</a>
    - <a href="#minor-changes-4">Minor Changes</a>

<a id="v0-2-2"></a>
## v0\.2\.2

<a id="minor-changes"></a>
### Minor Changes

* 1. <em class="title-reference">st\-cli upgrade</em> now checks upstream first and\, without pipx \(container installs\)\, tells the user to run <em class="title-reference">docker pull ghcr\.io/suitenumerique/st\-cli\:latest</em> and re\-run <em class="title-reference">st\-cli upgrade</em> instead of the pip hint
* 1. the behind\-upstream warning on other commands now branches on pipx — without pipx it names the exact <em class="title-reference">docker pull ghcr\.io/suitenumerique/st\-cli\:latest</em> command to run before <em class="title-reference">st\-cli upgrade</em>
* \(plugins\) the compact callback now prints the <em class="title-reference">msg</em> of a <em class="title-reference">debug</em> task in a green box in place of the ok line and the JSON dump
* Update docker\.io/clamav/clamav Docker tag to v1\.5\.4
* Update docker\.io/rspamd/rspamd Docker tag to v4\.1\.4
* Update docker\.io/valkey/valkey Docker tag to v9\.1\.1
* Update docker/login\-action digest to dbcb813

<a id="bugfixes"></a>
### Bugfixes

* 1. only push latest on new tags to follow the st\-cli collection tags
* \(plugins\) the compact callback no longer concatenates the pending task lines on one row when a task runs on multiple hosts\, and it keeps a live pending line while other hosts still run

<a id="v0-2-1"></a>
## v0\.2\.1

<a id="minor-changes-1"></a>
### Minor Changes

* \(callback\) added the suitenumerique\.st\.compact stdout callback\, one line per task and host\, a live progress line on a TTY\, diffs for changed tasks\, and full default\-style error output
* 1. the generated ansible\.cfg now selects suitenumerique\.st\.compact as the stdout callback
* 1. the generated ansible\.cfg silences the Python interpreter discovery warning with <em class="title-reference">interpreter\_python \= auto\_silent</em>

<a id="v0-2-0"></a>
## v0\.2\.0

<a id="release-summary"></a>
### Release Summary

Adds support for meet recordings\, fix openbao markers on st\-cli bootstrap
and multiple versions upgrades\.

<a id="minor-changes-2"></a>
### Minor Changes

* \(meet\) added custom logo handling
* \(meet\) separated egress component and added recording feature to bootstrap
* Update actions/checkout digest to 3d3c42e
* Update actions/setup\-python action to v7
* Update dependency ansible\.posix to v2\.2\.2
* Update dependency containers\.podman to v1\.20\.2
* Update dependency suitenumerique/meet to v1\.23\.0
* Update docker\.io/livekit/livekit\-server Docker tag to v1\.13\.4
* Update docker/login\-action digest to abd2ef4

<a id="bugfixes-1"></a>
### Bugfixes

* 1. allow \@openbao markers on non\-secret fields

<a id="v0-1-1"></a>
## v0\.1\.1

<a id="minor-changes-3"></a>
### Minor Changes

* Added Renovate configuration \(<code>renovate\.json5</code>\) and renovate Makefile target
* Pinned the st\-cli and molecule\-lima Python dependencies to exact versions
* Update actions/checkout action to v7
* Update dependency restic/restic to v0\.19\.1
* Update docker/build\-push\-action action to v7
* Update docker/login\-action action to v4
* Update docker/metadata\-action action to v6
* Update docker/setup\-buildx\-action action to v4
* Update docker/setup\-qemu\-action action to v4

<a id="bugfixes-2"></a>
### Bugfixes

* Fixed restic install task for upgrade workflow

<a id="v0-1-0"></a>
## v0\.1\.0

<a id="release-summary-1"></a>
### Release Summary

Added st\-cli to manage LST environments bootstraps and deployments\, refactored roles to make single\-host deployments easier\, started the CHANGELOG dance\.

<a id="major-changes"></a>
### Major Changes

* 1. added st\-cli \#27
* \(roles\) refactor every uid\, gid and ports to allow single\-host deployments \#26

<a id="minor-changes-4"></a>
### Minor Changes

* \(changelog\) added antsibull\-changelog config\, Makefile targets and CI job \#13
