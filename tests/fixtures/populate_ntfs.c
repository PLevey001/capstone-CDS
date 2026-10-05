/* Unprivileged Stage 6 fixture writer, not an application dependency.
 * Only opaque handles cross the ntfs-3g ABI; no private structure layouts.
 * Declarations: tuxera/ntfs-3g tag 2022.10.3, include/ntfs-3g/{dir,attrib,volume,inode}.h.
 */
#define _GNU_SOURCE
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

typedef struct _ntfs_volume ntfs_volume;
typedef struct _ntfs_inode ntfs_inode;
typedef struct _ntfs_attr ntfs_attr;
extern ntfs_volume *ntfs_mount(const char *, unsigned long);
extern int ntfs_umount(ntfs_volume *, int);
extern ntfs_inode *ntfs_pathname_to_inode(ntfs_volume *, ntfs_inode *, const char *);
extern ntfs_inode *ntfs_create(ntfs_inode *, uint32_t, const uint16_t *, uint8_t, mode_t);
extern int ntfs_delete(ntfs_volume *, const char *, ntfs_inode *, ntfs_inode *, const uint16_t *, uint8_t);
extern int ntfs_inode_close(ntfs_inode *);
extern int ntfs_inode_close_in_dir(ntfs_inode *, ntfs_inode *);
extern int ntfs_mbstoucs(const char *, uint16_t **);
extern void ntfs_ucsfree(uint16_t *);
extern int ntfs_set_char_encoding(const char *);
extern ntfs_attr *ntfs_attr_open(ntfs_inode *, uint32_t, uint16_t *, uint32_t);
extern int64_t ntfs_attr_pwrite(ntfs_attr *, int64_t, int64_t, const void *);
extern void ntfs_attr_close(ntfs_attr *);

/* ntfs-3g uses CLOCK_REALTIME for inode times. Fix only this writer's clock;
 * monotonic deadlines and the pytest process retain their real clocks.
 */
int clock_gettime(clockid_t clock, struct timespec *value)
{
    if (clock != CLOCK_REALTIME)
        return syscall(SYS_clock_gettime, clock, value);
    value->tv_sec = 1700000000;
    value->tv_nsec = 0;
    return 0;
}

static void require(int ok, const char *operation)
{
    if (!ok) {
        perror(operation);
        exit(1);
    }
}

int main(int argc, char **argv)
{
    require(argc >= 4, "usage: populate_ntfs image source-tree paths...");
    require(ntfs_set_char_encoding("UTF-8") == 0, "UTF-8");
    /* This library call opens a regular image file. It does not mount it in
     * the OS, require FUSE, allocate a loop device, or need root privileges.
     */
    ntfs_volume *volume = ntfs_mount(argv[1], 0);
    require(volume != NULL, "open image");
    for (int index = 3; index < argc; index++) {
        char *parent_path = strdup(argv[index]);
        char *basename = strrchr(parent_path, '/');
        require(basename != NULL, "absolute fixture path");
        uint16_t *name = NULL;
        int length = ntfs_mbstoucs(basename + 1, &name);
        require(length > 0 && length <= 255, "filename");
        *basename = '\0';
        ntfs_inode *parent = ntfs_pathname_to_inode(volume, NULL, *parent_path ? parent_path : "/");
        require(parent != NULL, "parent directory");
        char *source;
        require(asprintf(&source, "%s%s", argv[2], argv[index]) >= 0, "source path");
        struct stat info;
        require(stat(source, &info) == 0, "source stat");
        ntfs_inode *inode = ntfs_create(parent, 0, name, length, info.st_mode & S_IFMT);
        require(inode != NULL, "create");
        if (S_ISREG(info.st_mode)) {
            ntfs_attr *data = ntfs_attr_open(inode, 0x80, NULL, 0); /* unnamed $DATA */
            require(data != NULL, "open data");
            FILE *file = fopen(source, "rb");
            require(file != NULL, "source open");
            char buffer[65536];
            size_t size;
            int64_t offset = 0;
            while ((size = fread(buffer, 1, sizeof(buffer), file)) != 0) {
                require(ntfs_attr_pwrite(data, offset, size, buffer) == (int64_t)size, "write data");
                offset += size;
            }
            require(!ferror(file) && offset == info.st_size, "source read");
            require(fclose(file) == 0, "source close");
            ntfs_attr_close(data);
        }
        require(ntfs_inode_close_in_dir(inode, parent) == 0, "close inode in parent");
        require(ntfs_inode_close(parent) == 0, "close parent");
        ntfs_ucsfree(name);
        free(source);
        free(parent_path);
    }
    /* Delete last, after every allocation. Its resident bytes survive in the
     * freed MFT record; no later file can reuse that record in this fixture.
     * ntfs_delete closes both handles, including on failure.
     */
    ntfs_inode *parent = ntfs_pathname_to_inode(volume, NULL, "/");
    ntfs_inode *deleted = ntfs_pathname_to_inode(volume, NULL, "/deleted.txt");
    uint16_t *name = NULL;
    int length = ntfs_mbstoucs("deleted.txt", &name);
    require(parent != NULL && deleted != NULL && length > 0, "delete lookup");
    require(ntfs_delete(volume, "/deleted.txt", deleted, parent, name, length) == 0, "delete");
    ntfs_ucsfree(name);
    require(ntfs_umount(volume, 0) == 0, "close image");
    return 0;
}
