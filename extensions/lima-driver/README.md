# Molecule Lima Driver

A [Molecule](https://ansible.readthedocs.io/projects/molecule/) driver for [Lima VM](https://lima-vm.io/), enabling testing of Ansible roles on Linux with native virtualization support.

Very highly inspired by https://github.com/filatof/molecule-lima.git, but with less features and with defaults focused on our use case.

## Requirements

[Lima](https://lima-vm.io)

## Installation

### Install Lima

```bash
# Download the latest release
wget https://github.com/lima-vm/lima/releases/latest/download/lima-$(uname -m).tar.gz
tar -xzf lima-$(uname -m).tar.gz
sudo install -m 755 bin/limactl /usr/local/bin/
rm -rf lima-$(uname -m)*
```

### Install Molecule Lima Driver
```bash
pip install -e .
```

## Configuration Options

### Platform Parameters

| Parameter | Description | Default | Required |
|-----------|-------------|---------|----------|
| `name` | Instance name | - | Yes |
| `image` | OS image URL | See [default image](#default-image) | No |
| `cpus` | Number of CPUs | `2` | No |
| `memory` | RAM amount | `2GiB` | No |
| `disk` | Disk size | `20GiB` | No |
| `provision_script` | Provisioning bash script | - | No |
| `mounts` | Additional mount points | - | No |

### Default Image {#default-image}

The default OS image is `https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2`.

### Advanced Configuration Example
```yaml
driver:
  name: molecule-lima
  ssh_timeout: 240

platforms:
  - name: instance
    image: "https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2"
    cpus: 4
    memory: 4GiB
    disk: 30GiB
    python_interpreter: /usr/bin/python3
    provision_script: |
      apt-get update
      apt-get install -y docker.io python3-pip
      systemctl enable --now docker
      usermod -aG docker $USER
    mounts:
      - location: "/home/user/project"
        writable: true
```
