FFmpeg 7 DLLs for torchcodec (Windows)
======================================

torchcodec (used by OmniVoice to load audio) needs FFmpeg shared libraries.
On Windows it supports FFmpeg 4-7 only. FFmpeg 8 will NOT be detected.

Download a FFmpeg 7.x 'shared' build and copy ONLY these 7 DLLs into this folder:
    avutil-59.dll  avcodec-61.dll  avformat-61.dll  avdevice-61.dll
    avfilter-10.dll  swscale-8.dll  swresample-5.dll

(avutil-60 / avcodec-62 = FFmpeg 8 = wrong version.)
install_omnivoice.bat copies these next to python.exe automatically.
