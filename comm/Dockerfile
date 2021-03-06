# SPDX-License-Identifier: Apache-2.0
FROM ubuntu:20.04 as jail-root

ENV DEBIAN_FRONTEND=noninteractive

RUN yes | unminimize
RUN apt-get update && \
    apt-get install -y build-essential autoconf bison flex \
                       git libtool pkg-config \
                       htop strace lsof man-db \
                       vim nano emacs \
                       curl wget \
                       iputils-ping iproute2 \
                       python-is-python3

RUN touch /dev/discord

FROM ubuntu:20.04 as ubuntu-build

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y build-essential flex bison git pkg-config \
                       libprotobuf-dev protobuf-compiler \
                       libnl-route-3-dev

RUN git clone https://github.com/google/nsjail.git /nsjail -b 3.0 --depth 1
RUN cd /nsjail && make -j$(nproc)

COPY nsjail-hooks.c /
RUN gcc -shared -fPIC -o nsjail-hooks.so nsjail-hooks.c -ldl

FROM alpine as alpine-build

RUN apk update && \
    apk add build-base libcap-static gettext-static

COPY bash /bash
RUN cd /bash && ./configure --enable-static-link --without-bash-malloc && \
    make -j$(nproc)

FROM ubuntu:20.04 as comm

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y libprotobuf-dev libnl-route-3-dev slirp4netns && \
    apt-get install -y software-properties-common && \
    add-apt-repository ppa:deadsnakes/ppa && \
    apt-get install -y python3.9 python3.9-venv

RUN mkdir /home/user/

RUN python3.9 -m venv /home/user/venv
RUN /home/user/venv/bin/pip install pyte fuse-python

RUN mkdir /run/discord-upload-fuse
RUN mkdir /run/container-run

COPY --from=jail-root / /jailroot
COPY --from=alpine-build /bash/bash /home/user/
COPY --from=ubuntu-build /nsjail/nsjail /home/user/
COPY --from=ubuntu-build /nsjail-hooks.so /home/user/

COPY nsjail.cfg comm.py jail.sh /home/user/

CMD ["/home/user/venv/bin/python3.9", "/home/user/comm.py"]
