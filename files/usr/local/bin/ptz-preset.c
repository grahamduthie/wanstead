#include <stdio.h>
#include <stdlib.h>
#include <fcntl.h>
#include <unistd.h>
#include <string.h>
#include <sys/ioctl.h>
#include <linux/videodev2.h>
#include <linux/uvcvideo.h>

#define DEVICE "/dev/video0"

#define UVC_GET_CUR  0x01
#define UVC_GET_MIN  0x02
#define UVC_GET_MAX  0x03
#define UVC_GET_RES  0x04
#define UVC_GET_DEF  0x05
#define UVC_GET_INFO 0x06
#define UVC_GET_LEN  0x07
#define UVC_SET_CUR  0x01

#define LOGITECH_XU_PERIPHERAL_CONTROL_UNIT 11
#define XU_PANTILT_MODE_CONTROL_SELECTOR 0x02

#define CMD_GOTO_HOME      3
#define CMD_SAVE_PRESET_1 4
#define CMD_SAVE_PRESET_2 5
#define CMD_SAVE_PRESET_3 6
#define CMD_GOTO_PRESET_1 12
#define CMD_GOTO_PRESET_2 13
#define CMD_GOTO_PRESET_3 14

int send_xu_command(int fd, __u8 value) {
    struct uvc_xu_control_query xu_query;
    __u8 data[4] = {0};
    
    data[0] = value & 0xFF;
    
    xu_query.unit = LOGITECH_XU_PERIPHERAL_CONTROL_UNIT;
    xu_query.selector = XU_PANTILT_MODE_CONTROL_SELECTOR;
    xu_query.query = UVC_SET_CUR;
    xu_query.size = 1;
    xu_query.data = data;
    
    if (ioctl(fd, UVCIOC_CTRL_QUERY, &xu_query) < 0) {
        return -1;
    }
    return 0;
}

int main(int argc, char *argv[]) {
    int fd;
    int cmd;
    
    if (argc < 2) {
        fprintf(stderr, "Usage: %s <command>\n", argv[0]);
        fprintf(stderr, "Commands:\n");
        fprintf(stderr, "  home       - Go to home position\n");
        fprintf(stderr, "  save1      - Save current position as Preset 1\n");
        fprintf(stderr, "  save2      - Save current position as Preset 2\n");
        fprintf(stderr, "  save3      - Save current position as Preset 3\n");
        fprintf(stderr, "  preset1    - Go to Preset 1\n");
        fprintf(stderr, "  preset2    - Go to Preset 2\n");
        fprintf(stderr, "  preset3    - Go to Preset 3\n");
        return 1;
    }
    
    fd = open(DEVICE, O_RDWR);
    if (fd < 0) {
        perror("open");
        return 1;
    }
    
    if (strcmp(argv[1], "home") == 0) {
        cmd = CMD_GOTO_HOME;
    } else if (strcmp(argv[1], "save1") == 0) {
        cmd = CMD_SAVE_PRESET_1;
    } else if (strcmp(argv[1], "save2") == 0) {
        cmd = CMD_SAVE_PRESET_2;
    } else if (strcmp(argv[1], "save3") == 0) {
        cmd = CMD_SAVE_PRESET_3;
    } else if (strcmp(argv[1], "preset1") == 0) {
        cmd = CMD_GOTO_PRESET_1;
    } else if (strcmp(argv[1], "preset2") == 0) {
        cmd = CMD_GOTO_PRESET_2;
    } else if (strcmp(argv[1], "preset3") == 0) {
        cmd = CMD_GOTO_PRESET_3;
    } else {
        fprintf(stderr, "Unknown command: %s\n", argv[1]);
        close(fd);
        return 1;
    }
    
    if (send_xu_command(fd, cmd) < 0) {
        perror("UVCIOC_CTRL_QUERY");
        close(fd);
        return 1;
    }
    
    printf("Command '%s' (value=%d) executed successfully\n", argv[1], cmd);
    close(fd);
    return 0;
}
