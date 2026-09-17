package airplay

// The app's media worker owns the tuner, decoding and shared encoders. Each
// receiver reads timestamped RTP from an owner-only Unix socket, retaining the
// normal AirPlay pairing, framing, encryption and negotiated audio encoder.
import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"time"
)

func dialMedia(ctx context.Context, path, kind string, width, height int) (net.Conn, error) {
	conn, err := (&net.Dialer{Timeout: 5 * time.Second}).DialContext(ctx, "unix", path)
	if err != nil {
		return nil, fmt.Errorf("connect media worker: %w", err)
	}
	conn.SetDeadline(time.Now().Add(10 * time.Second))
	err = json.NewEncoder(conn).Encode(map[string]any{"kind": kind, "width": width, "height": height})
	var ready [1]byte
	if err == nil {
		_, err = io.ReadFull(conn, ready[:])
	}
	if err != nil || ready[0] != 1 {
		conn.Close()
		return nil, fmt.Errorf("media worker could not prepare %s", kind)
	}
	conn.SetDeadline(time.Time{})
	return conn, nil
}

// StartMediaVideo subscribes to the encoder matching the negotiated canvas.
// RTP/ONVIF preserves the worker's shared audio/video presentation clock.
func StartMediaVideo(ctx context.Context, path string, width, height int) (*ScreenCapture, error) {
	conn, err := dialMedia(ctx, path, "video", width, height)
	if err != nil {
		return nil, err
	}
	lifetime, cancel := context.WithCancel(ctx)
	capture := &ScreenCapture{stdout: conn, frames: newRTPVideoAccessUnitReader(conn, VideoCodecH264), cancel: cancel, waitCh: make(chan struct{})}
	go func() { <-lifetime.Done(); conn.Close(); close(capture.waitCh) }()
	return capture, nil
}

// StartMediaAudio subscribes to shared PCM and encodes it for this receiver.
// Independent readers can assemble ALAC's 352 or AAC-ELD's 480 sample frames.
func StartMediaAudio(ctx context.Context, path string, codec AudioCodec) (*AudioCapture, error) {
	if codec != AudioCodecALAC && codec != AudioCodecAACELD {
		return nil, fmt.Errorf("unsupported audio codec")
	}
	conn, err := dialMedia(ctx, path, "audio", 0, 0)
	if err != nil {
		return nil, err
	}
	lifetime, cancel := context.WithCancel(ctx)
	capture := &AudioCapture{pcmPipe: conn, pcmFrames: newRTPL16PCMFrameReader(conn), codec: codec, cancel: cancel, waitCh: make(chan struct{})}
	if codec == AudioCodecAACELD {
		capture.eld, err = newELDEncoder()
		if err != nil {
			cancel()
			conn.Close()
			return nil, err
		}
	}
	go func() { <-lifetime.Done(); conn.Close(); close(capture.waitCh) }()
	return capture, nil
}
