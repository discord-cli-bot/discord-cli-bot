#define _GNU_SOURCE

#include <dlfcn.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <unistd.h>

static int (*real_pause)(void);
static int (*real_mount)(const char *source, const char *target,
			 const char *filesystemtype, unsigned long mountflags,
			 const void *data);

static int exefd;
static int sock;

__attribute__((constructor))
static void init(void)
{
	char *sock_fd_env;
	char *exe_fd_env;

	real_pause = dlsym(RTLD_NEXT, "pause");
	if (!real_pause)
		exit(1);
	real_mount = dlsym(RTLD_NEXT, "mount");
	if (!real_mount)
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

int pause(void)
{
	static atomic_flag closed;

	if (!atomic_flag_test_and_set(&closed)) {
		close(sock);
		close(exefd);
	}

	return real_pause();
}

int mount(const char *source, const char *target,
	  const char *filesystemtype, unsigned long mountflags,
	  const void *data)
{
	if (source && !strcmp(source, "/dev/discord") &&
	    target && !strcmp(target, "/dev/discord") &&
	    !filesystemtype && mountflags & MS_REMOUNT)
		return 0;

	return real_mount(source, target, filesystemtype, mountflags, data);
}
