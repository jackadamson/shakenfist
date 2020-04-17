FROM python:3.7-stretch
RUN apt-get update && apt-get install -y \
    bridge-utils \
    default-libmysqlclient-dev \
    dnsutils \
    git \
    libssl-dev \
    libvirt-daemon-system \
    libvirt-dev \
    mariadb-client \
    mysql-client \
    net-tools \
    pwgen \
    python-libvirt \
    python3-libvirt \
    qemu-kvm \
  && rm -rf /var/lib/apt/lists/*

# Fast way to get docker
RUN curl -fsSL https://get.docker.com -o get-docker.sh
RUN bash get-docker.sh
WORKDIR /srv/shakenfist/src

# Install requirements to save time with future builds where dependencies have not changed
COPY requirements.txt ./
RUN pip install -r requirements.txt
RUN pip install libvirt-python

# Copy the rest of the app and install
COPY setup.cfg setup.py README.md ./
COPY shakenfist ./shakenfist

# pbr requires this to be a git repo...
COPY .git ./.git
RUN pip install -e .

WORKDIR /srv/shakenfist

EXPOSE 13000

CMD ["sf-daemon"]
