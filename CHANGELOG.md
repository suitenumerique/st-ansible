# suitenumerique\.st Release Notes

**Topics**

- <a href="#v0-4-0">v0\.4\.0</a>
    - <a href="#release-summary">Release Summary</a>
    - <a href="#minor-changes">Minor Changes</a>
    - <a href="#bugfixes">Bugfixes</a>
- <a href="#v0-3-1">v0\.3\.1</a>
    - <a href="#minor-changes-1">Minor Changes</a>
- <a href="#v0-3-0">v0\.3\.0</a>
    - <a href="#release-summary-1">Release Summary</a>
    - <a href="#minor-changes-2">Minor Changes</a>
    - <a href="#bugfixes-1">Bugfixes</a>
    - <a href="#new-plugins">New Plugins</a>
        - <a href="#callback">Callback</a>
- <a href="#v0-2-2">v0\.2\.2</a>
    - <a href="#minor-changes-3">Minor Changes</a>
    - <a href="#bugfixes-2">Bugfixes</a>
- <a href="#v0-2-1">v0\.2\.1</a>
    - <a href="#minor-changes-4">Minor Changes</a>
- <a href="#v0-2-0">v0\.2\.0</a>
    - <a href="#release-summary-2">Release Summary</a>
    - <a href="#minor-changes-5">Minor Changes</a>
    - <a href="#bugfixes-3">Bugfixes</a>
- <a href="#v0-1-1">v0\.1\.1</a>
    - <a href="#minor-changes-6">Minor Changes</a>
    - <a href="#bugfixes-4">Bugfixes</a>
- <a href="#v0-1-0">v0\.1\.0</a>
    - <a href="#release-summary-3">Release Summary</a>
    - <a href="#major-changes">Major Changes</a>
    - <a href="#minor-changes-7">Minor Changes</a>

<a id="v0-4-0"></a>
## v0\.4\.0

<a id="release-summary"></a>
### Release Summary

Added cli rebootstrap and improve upgrades\, added an IP filtering setup in front of Django Admins paths\, bump drive\, docs and meet\.

<a id="minor-changes"></a>
### Minor Changes

* Update dependency suitenumerique/docs to v5\.6\.1
* Update dependency suitenumerique/drive to v0\.22\.0
* Update dependency suitenumerique/meet to v1\.31\.0
* Update docker\.io/collabora/code Docker tag to 26\.04\.3\.2\.1
* Update docker/build\-push\-action digest to c3c9e26
* Update docker/setup\-buildx\-action digest to f87e599
* Update docker/setup\-qemu\-action digest to 9901266
* cli\: <code>\@openbao\(\)</code> markers typed into a provider prompt are expanded\. <code>st\-cli version</code> prints the pin warning once\.
* cli\: <code>doctor</code> diffs the committed env blobs against the current templates and no longer reports <code>st\_\*</code> vars absent from the role spec\.
* cli\: <code>st\-cli bootstrap</code> on an existing unit replays the questionnaire pre\-filled from the committed tree and merges the result\. Secrets are never rotated\.
* cli\: <code>st\-cli deploy</code> resolves every target host before it deploys a component\. A wrong <code>\-c</code> and <code>\-H</code> pair or an empty inventory now fails before any deploy\.
* cli\: <code>st\-cli upgrade</code> no longer runs <code>pipx upgrade</code> itself\. It prints the install command and stops\.
* cli\: <code>upgrades\.yml</code> accepts <code>version\: next</code> for the flag of an unreleased change\. <code>make version</code> replaces it with the release version\. A <code>next</code> flag replays on every <code>st\-cli upgrade</code> run and never blocks <code>deploy</code>\.
* cli\: <code>upgrades\.yml</code> declares per\-release rebootstrap flags \(<code>apps</code>\, <code>components</code>\, <code>full\_replay</code>\, <code>warnings</code>\) and a <code>baseline</code> stamp\. <code>doctor</code> reports them\, <code>deploy</code> refuses to run until they are cleared\, <code>st\-cli upgrade</code> replays the flagged units and prints the manual steps\.
* cli\: fix the README\, <code>CLAUDE\.md</code> and help texts\. <code>podman rmi</code> removes the image\. <code>st\-cli upgrade</code> does not upgrade the CLI itself\. <code>ST\_CLI\_NO\_UPSTREAM\_CHECK</code> also disables the upstream gate of <code>st\-cli upgrade</code>\.
* cli\: remove dead code\, merge duplicate helpers\, and trim the docstrings to the why\-only rule\. <code>pyproject\.toml</code> commits the ruff rule set\. The <code>ansible</code> extra pins the same <code>ansible\-core</code> as <code>full</code>\.
* cli\: the <code>link</code> key of a flag is optional\. The CLI derives the CHANGELOG anchor from the flag version\.
* cli\: the drive bootstrap writes <code>st\_drive\_caddy\_env</code> and literal S3 values\, like meet and docs\. A 0\.4\.0 flag makes <code>st\-cli upgrade</code> replay drive and print the manual steps\.
* docs\, drive\, meet\: the caddy edge now walks <code>X\-Forwarded\-For</code> right to left \(<code>trusted\_proxies\_strict</code>\)\. A client can no longer set its own client IP with a prepended <code>X\-Forwarded\-For</code> entry\. Django still receives the same <code>X\-Forwarded\-For</code> header as before\.
* docs\, drive\, meet\: the caddy edge reads two optional keys in <code>st\_\<app\>\_caddy\_env</code>\: <code>CADDY\_ADMIN\_IP\_ALLOWLIST</code> \(space\-separated CIDR list of client IPs allowed on <code>/admin</code>\, default allows all\) and <code>CADDY\_TRUSTED\_PROXIES</code> \(proxies whose <code>X\-Forwarded\-For</code> sets the client IP\, default <code>private\_ranges</code>\, the previous hard\-coded value\)\.
* docs\: the example playbooks set the caddy env for drive and meet\. Renovate groups the caddy bumps in one PR\.
* drive\: a <code>drive\-caddy</code> container now owns <code>st\_drive\_port</code> and proxies the frontend\, the backend and the S3 media routes\, like docs and meet\. <code>st\_drive\_nginx\_template</code> and the <code>st\_drive\_s3\_\*</code> vars are replaced by <code>st\_drive\_caddy\_env</code> \(<code>CADDY\_S3\_\*</code>\)\, <code>st\_drive\_caddy\_image</code> and <code>st\_drive\_caddy\_tag</code>\.
* drive\: the collabora compose file no longer sets a healthcheck\. The 26\.04\.3 image has no shell\, and podman\-compose 1\.3\.0 wraps every compose test in <code>/bin/sh \-c</code>\. Podman applies the <code>HEALTHCHECK</code> of the image \(<code>coolwsd \-\-probe</code>\) instead\. A custom <code>st\_drive\_collabora\_compose\_template</code> must drop its <code>healthcheck</code> block too\.

<a id="bugfixes"></a>
### Bugfixes

* cli\: <code>st\-cli reset \-H \<alias\></code> redeploys only the selected host\. It redeployed the component on every host\.
* cli\: a blank <code>AWS\_S3\_REGION\_NAME</code> answer on a rebootstrap removes the key and warns\, instead of writing an empty value\.

<a id="v0-3-1"></a>
## v0\.3\.1

<a id="minor-changes-1"></a>
### Minor Changes

* Update dependency suitenumerique/docs to v5\.5\.0
* Update dependency suitenumerique/meet to v1\.29\.0
* Update dependency typer to v0\.27\.2
* Update docker\.io/livekit/livekit\-server Docker tag to v1\.13\.6
* Update docker/setup\-buildx\-action digest to 37fe631

<a id="v0-3-0"></a>
## v0\.3\.0

<a id="release-summary-1"></a>
### Release Summary

Adds two new applications\: Docs \(impress\, with workers and yprovider
sub\-apps\) and Projects \(a Planka fork\)\, both deployable end to end with
st\-cli\. The bootstrap \"Requirements\" checklist is now app\-aware\, and
several dependencies were bumped \(meet 1\.27\.0\, messages 0\.9\.0\, LiveKit\)\.

<a id="minor-changes-2"></a>
### Minor Changes

* 1. added docs app support to st\-cli
* \(docs\) added the docs role\, deploying the Docs \(impress\) application as a caddy \+ frontend \+ backend core\, with workers and yprovider sub\-apps
* \(projects\) new <code>projects</code> role and st\-cli support to deploy Projects \(<code>suitenumerique/projects</code>\, a Planka fork\) — a single Sails\.js container with external PostgreSQL\, OIDC\-enforced SSO \(Keycloak/ProConnect provider switch\) and optional S3\-backed uploads\; <code>st\-cli bootstrap projects \<env\></code> / <code>st\-cli deploy projects \<env\></code> behave like the other apps\.
* \(st\-cli\) the bootstrap \"Requirements\" checklist is now app\-aware — each app declares the external infrastructure it needs via a <code>requires</code> manifest key — so <code>projects</code> and <code>keycloak</code> operators are no longer told to provision a Redis they never use\.
* Update dependency molecule to v26\.8\.0
* Update dependency suitenumerique/meet to v1\.27\.0
* Update dependency suitenumerique/messages to v0\.9\.0
* Update dependency typer to v0\.27\.1
* Update docker\.io/livekit/egress Docker tag to v1\.14\.1
* Update docker\.io/livekit/livekit\-server Docker tag to v1\.13\.5

<a id="bugfixes-1"></a>
### Bugfixes

* \(st\-cli\) the <code>vars\.yml</code> header no longer claims secrets are stored in an encrypted <code>vault\.yml</code> when the hashi\_vault \(OpenBao\) backend is in use\, where no <code>vault\.yml</code> is written and secrets are referenced by lookup\.

<a id="new-plugins"></a>
### New Plugins

<a id="callback"></a>
#### Callback

* suitenumerique\.st\.compact \- Compact one\-line\-per\-task output with a live pending line on a TTY\.

<a id="v0-2-2"></a>
## v0\.2\.2

<a id="minor-changes-3"></a>
### Minor Changes

* Update docker\.io/clamav/clamav Docker tag to v1\.5\.4
* Update docker\.io/rspamd/rspamd Docker tag to v4\.1\.4
* Update docker\.io/valkey/valkey Docker tag to v9\.1\.1
* Update docker/login\-action digest to dbcb813
* cli\: <code>st\-cli upgrade</code> now checks upstream first and\, without pipx \(container installs\)\, tells the user to run <code>docker pull ghcr\.io/suitenumerique/st\-cli\:latest</code> and re\-run <code>st\-cli upgrade</code> instead of the pip hint
* cli\: the behind\-upstream warning on other commands now branches on pipx — without pipx it names the exact <code>docker pull ghcr\.io/suitenumerique/st\-cli\:latest</code> command to run before <code>st\-cli upgrade</code>
* plugins\: the compact callback now prints the <code>msg</code> of a <code>debug</code> task in a green box in place of the ok line and the JSON dump

<a id="bugfixes-2"></a>
### Bugfixes

* cli\: only push latest on new tags to follow the st\-cli collection tags
* plugins\: the compact callback no longer concatenates the pending task lines on one row when a task runs on multiple hosts\, and it keeps a live pending line while other hosts still run

<a id="v0-2-1"></a>
## v0\.2\.1

<a id="minor-changes-4"></a>
### Minor Changes

* callback\: added the suitenumerique\.st\.compact stdout callback\, one line per task and host\, a live progress line on a TTY\, diffs for changed tasks\, and full default\-style error output
* cli\: the generated ansible\.cfg now selects suitenumerique\.st\.compact as the stdout callback
* cli\: the generated ansible\.cfg silences the Python interpreter discovery warning with <code>interpreter\_python \= auto\_silent</code>

<a id="v0-2-0"></a>
## v0\.2\.0

<a id="release-summary-2"></a>
### Release Summary

Adds support for meet recordings\, fix openbao markers on st\-cli bootstrap
and multiple versions upgrades\.

<a id="minor-changes-5"></a>
### Minor Changes

* Update actions/checkout digest to 3d3c42e
* Update actions/setup\-python action to v7
* Update dependency ansible\.posix to v2\.2\.2
* Update dependency containers\.podman to v1\.20\.2
* Update dependency suitenumerique/meet to v1\.23\.0
* Update docker\.io/livekit/livekit\-server Docker tag to v1\.13\.4
* Update docker/login\-action digest to abd2ef4
* meet\: added custom logo handling
* meet\: separated egress component and added recording feature to bootstrap

<a id="bugfixes-3"></a>
### Bugfixes

* cli\: allow \@openbao markers on non\-secret fields

<a id="v0-1-1"></a>
## v0\.1\.1

<a id="minor-changes-6"></a>
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

<a id="bugfixes-4"></a>
### Bugfixes

* Fixed restic install task for upgrade workflow

<a id="v0-1-0"></a>
## v0\.1\.0

<a id="release-summary-3"></a>
### Release Summary

Added st\-cli to manage LST environments bootstraps and deployments\, refactored roles to make single\-host deployments easier\, started the CHANGELOG dance\.

<a id="major-changes"></a>
### Major Changes

* cli\: added st\-cli \#27
* roles\: refactor every uid\, gid and ports to allow single\-host deployments \#26

<a id="minor-changes-7"></a>
### Minor Changes

* changelog\: added antsibull\-changelog config\, Makefile targets and CI job \#13
