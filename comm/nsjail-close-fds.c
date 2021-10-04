#define _GNU_SOURCE

#include <dlfcn.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static int (*real_pause)(void);

static int exefd;
static int sock;

__attribute__((constructor))
static void init(void) {
	char *sock_fd_env;
	char *exe_fd_env;

	real_pause = dlsym(RTLD_NEXT, "pause");
	if (!real_pause)
		exit(1);

	sock_fd_env = getenv("SOCK_FD");
	if (!sock_fd_env)
		exit(1);

	sock = atoi(sock_fd_env);

	exe_fd_env = getenv("EXE_FD");
	if (!exe_fd_env)
		exit(1);

	exefd = atoi(exe_fd_env);
}

int pause(void) {
	static atomic_flag closed;

	if (!atomic_flag_test_and_set(&closed)) {
		close(sock);
		close(exefd);
	}

	return real_pause();
}
