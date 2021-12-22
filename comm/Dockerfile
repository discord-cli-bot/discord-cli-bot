# SPDX-License-Identifier: Apache-2.0
FROM ubuntu:20.04 as jail-root

ENV DEBIAN_FRONTEND=noninteractive

RUN yes | unminimize
RUN apt-get update && \
    apt-get install -y build-essential autoconf bison flex \
                       git libtool pkg-config \
                       htop strace lsof man-db \
                       vim nano emacs \
                       iputils-ping iproute2

RUN touch /dev/discord

FROM ubuntu:20.04 as ubuntu-build

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y build-essential autoconf bison flex \
                       git libtool pkg-config

COPY bash /bash
RUN cd /bash && ./configure --enable-static-link && make -j$(nproc)

RUN apt-get install -y libprotobuf-dev protobuf-compiler \
                       libnl-route-3-dev

RUN git clone https://github.com/google/nsjail.git /nsjail -b 3.0 --depth 1
RUN cd /nsjail && make -j$(nproc)

COPY nsjail-hooks.c /
RUN gcc -shared -fPIC -o nsjail-hooks.so nsjail-hooks.c -ldl

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

COPY --from=jail-root / /jailroot
COPY --from=ubuntu-build /bash/bash /home/user/
COPY --from=ubuntu-build /nsjail/nsjail /home/user/
COPY --from=ubuntu-build /nsjail-hooks.so /home/user/

COPY nsjail.cfg comm.py jail.sh /home/user/

CMD ["/home/user/venv/bin/python3.9", "/home/user/comm.py"]
