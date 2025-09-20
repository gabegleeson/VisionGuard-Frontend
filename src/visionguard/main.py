import cv2
import numpy as np
import time
import json
import os
from datetime import datetime
from collections import deque
from skimage.metrics import structural_similarity as ssim
from ui_camera_picker import pick_camera_source


class HybridCameraQualityMonitor:
    def __init__(self, camera_source, save_alerts=True):
        """
        Hybrid Camera Quality Monitor combining best approaches
        """
        self.camera_source = camera_source
        self.save_alerts = save_alerts
        self.reference_frame = None
        self.reference_gray = None
        self.ref_brightness = 0
        self.running = False
        
        # Quality thresholds from both approaches
        self.blur_threshold = 100
        self.darkness_threshold = 50
        self.darkness_coverage_limit = 0.3
        self.red_dominance_threshold = 0.6
        self.tile_blur_threshold = 50
        self.tile_blur_ratio = 0.6
        self.ssim_threshold = 0.90
        
        # Time-based SSIM checking (ChatGPT's approach)
        self.ssim_check_interval = 0.5
        self.ssim_window = deque(maxlen=6)
        self.last_ssim_check = time.time()
        self.brightness_pause_until = 0
        
        # Fog/weather detection variables
        self.brightness_history = deque(maxlen=20)
        self.contrast_history = deque(maxlen=20)
        self.weather_pause_until = 0
        
        # Alert management
        self.last_alerts = {'blur': 0, 'darkness': 0, 'color': 0, 'tiles': 0, 'ssim': 0}
        self.alert_cooldown = 5
        
        # Create directories
        if self.save_alerts:
            os.makedirs('alerts', exist_ok=True)
            os.makedirs('reference', exist_ok=True)

        # Frame scan rate limiters
        self.frame_skip = 5 # Process every 5th frame
        self.frame_count = 0 # Keeps track of number of frames seen
    

    
    def connect_camera(self):
        """Connect to USB (int) or RTSP (str), applying desired resolution when possible."""
        print("🎬 Connecting...")
        desired = getattr(self, "desired_resolution", None)

        def _axis_upsert_resolution(url: str, wh):
            # Only touch Axis-style URLs
            if "/axis-media/media.amp" not in url:
                return url
            # add or replace resolution=WxH
            import urllib.parse as up
            parts = up.urlsplit(url)
            q = up.parse_qsl(parts.query, keep_blank_values=True)
            q = [(k, v) for (k, v) in q if k.lower() != "resolution"]
            if wh and len(wh) == 2:
                q.append(("resolution", f"{wh[0]}x{wh[1]}"))
            new_query = up.urlencode(q)
            return up.urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))

        if isinstance(self.camera_source, int):
            # USB webcam
            try:
                self.cap = cv2.VideoCapture(self.camera_source)
                if desired:
                    w, h = desired
                    self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
                    self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
            except Exception as e:
                raise Exception(f"USB camera open failed: {e}")
        else:
            # RTSP/IP camera
            url = self.camera_source
            if desired:
                url = _axis_upsert_resolution(url, desired)

            # Try lower subtype first (your existing strategy)
            url_low = url.replace("subtype=0", "subtype=2")
            print(f"📡 Trying low res stream: {url_low}")
            self.cap = cv2.VideoCapture(url_low)
            if not self.cap.isOpened():
                print("📡 subtype=2 failed, trying subtype=1...")
                url_main = url.replace("subtype=0", "subtype=1")
                self.cap = cv2.VideoCapture(url_main)
            if not self.cap.isOpened():
                print("📡 Falling back to original URL...")
                self.cap = cv2.VideoCapture(url)

            # Note: Most RTSP backends ignore CAP_PROP_* size. URL params are preferred.

        if not self.cap.isOpened():
            raise Exception("Cannot connect to camera")

        ret, frame = self.cap.read()
        if not ret or frame is None:
            raise Exception("Cannot read frames")
        print(f"✅ Connected: {frame.shape[1]}x{frame.shape[0]}")
        return True



    
    def calculate_blur(self, frame):
        """ChatGPT's blur calculation"""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return cv2.Laplacian(gray, cv2.CV_64F).var()
    
    def check_dark_coverage(self, frame):
        """ChatGPT's darkness detection"""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        dark_pixels = np.sum(gray < self.darkness_threshold)
        total_pixels = gray.size
        dark_ratio = dark_pixels / total_pixels
        return dark_ratio > self.darkness_coverage_limit, dark_ratio
    
    def is_color_dominant(self, frame):
        """ChatGPT's color dominance detection"""
        b, g, r = cv2.split(frame)
        total_pixels = frame.shape[0] * frame.shape[1]
        dominant_pixels = np.sum((r > 100) & (r > g + 40) & (r > b + 40))
        return (dominant_pixels / total_pixels) > self.red_dominance_threshold
    
    def detect_blurry_tiles(self, frame):
        """ChatGPT's tile-based blur detection"""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape
        grid_size = (4, 4)
        tile_h, tile_w = h // grid_size[0], w // grid_size[1]
        blurry_count = 0
        total_tiles = grid_size[0] * grid_size[1]
        
        for i in range(grid_size[0]):
            for j in range(grid_size[1]):
                tile = gray[i * tile_h:(i + 1) * tile_h, j * tile_w:(j + 1) * tile_w]
                blur = cv2.Laplacian(tile, cv2.CV_64F).var()
                if blur < self.tile_blur_threshold:
                    blurry_count += 1
        
        ratio = blurry_count / total_tiles
        return ratio > self.tile_blur_ratio, ratio
    
    def calculate_ssim_score(self, current_gray):
        """ChatGPT's SSIM calculation"""
        if self.reference_gray is None:
            return 1.0
        score, _ = ssim(self.reference_gray, current_gray, full=True)
        return score
    
    def average_brightness(self, gray_frame):
        """Calculate average brightness of grayscale frame"""
        return np.mean(gray_frame)
    
    def detect_fog_conditions(self, frame):
        """Detect if current conditions indicate fog/weather rather than obstruction"""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        
        # Calculate image statistics
        mean_brightness = np.mean(gray)
        std_deviation = np.std(gray)
        
        # Calculate contrast using standard deviation
        contrast_ratio = std_deviation / mean_brightness if mean_brightness > 0 else 0
        
        # Fog indicators
        is_high_brightness = mean_brightness > 120
        is_low_contrast = contrast_ratio < 0.3
        is_uniform_distribution = std_deviation < 40
        
        # Check if this looks like fog
        fog_score = 0
        if is_high_brightness: 
            fog_score += 1
        if is_low_contrast: 
            fog_score += 1  
        if is_uniform_distribution: 
            fog_score += 1
        
        is_fog_likely = fog_score >= 2
        
        fog_details = {
            'brightness': mean_brightness,
            'contrast_ratio': contrast_ratio,
            'std_dev': std_deviation,
            'fog_score': fog_score,
            'is_fog': is_fog_likely
        }
        
        return is_fog_likely, fog_details
    
    def detect_weather_gradual_change(self):
        """Track if changes are gradual (weather) vs sudden (obstruction)"""
        if len(self.brightness_history) < 5:
            return False
        
        # Check if change is gradual (weather) vs sudden (obstruction)
        brightness_changes = [abs(self.brightness_history[i] - self.brightness_history[i-1]) 
                            for i in range(1, len(self.brightness_history))]
        
        avg_change = np.mean(brightness_changes) if brightness_changes else 0
        max_change = max(brightness_changes) if brightness_changes else 0
        
        # Gradual change (weather): small consistent changes
        is_gradual = max_change < 30 and avg_change < 10
        
        return is_gradual
    
    def trigger_alert(self, alert_type, details):
        """Quick alert system"""
        current_time = time.time()
        
        if current_time - self.last_alerts[alert_type] < self.alert_cooldown:
            return
        
        self.last_alerts[alert_type] = current_time
        print(f"🚨 ALERT: {alert_type.upper()} - {details}")
    
    def capture_reference_frame(self):
        """Capture reference frame with brightness baseline"""
        ret, frame = self.cap.read()
        if ret:
            self.reference_frame = frame.copy()
            self.reference_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            self.ref_brightness = self.average_brightness(self.reference_gray)
            
            if self.save_alerts:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                cv2.imwrite(f'reference/reference_{timestamp}.jpg', frame)
            
            print(f"✅ Reference captured. Brightness: {self.ref_brightness:.2f}")
            return True
        return False
    
    def run_monitoring(self):
        """Hybrid monitoring loop"""
        if not self.connect_camera():
            return
        
        print("\n" + "="*60)
        print("🔥 HYBRID CAMERA QUALITY MONITOR")
        print("="*60)
        print("🎯 Combining ChatGPT + Claude approaches")
        print("📊 Multiple detection methods active")
        print("🌫️ Weather-aware fog detection")
        print("\nDetection Methods:")
        print("• Overall blur detection")
        print("• Darkness/obstruction coverage") 
        print("• Color dominance (vandalism)")
        print("• Tile-based blur analysis")
        print("• SSIM scene change detection")
        print("• Fog/weather compensation")
        print("\nControls: r=reference, q=quit")
        print("="*60)
        
        # Capture reference
        print("📸 Capturing reference frame...")
        time.sleep(2)
        self.capture_reference_frame()
        
        self.running = True
        frame_count = 0
        
        cv2.namedWindow('Hybrid Monitor', cv2.WINDOW_NORMAL)
        cv2.resizeWindow('Hybrid Monitor', 1000, 700)
        
        while self.running:
            start_time = time.time()
            
            ret, frame = self.cap.read()
            if not ret:
                print("⚠️ Frame read failed")
                continue
            

            
            
            frame_count += 1

            # Code for limiting how many frames are skipped between scans. Currently untested. Primitive and should probably be replaced.
            if self.frame_count % self.frame_skip != 0:
                # Just show the frame, skip heavy checks
                cv2.putText(frame, "SKIPPED (frame_skip)", (10, 200),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
                cv2.imshow(self.window_name, frame)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    self.running = False
                continue  # go to next loop iteration


            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            current_brightness = self.average_brightness(gray)
            
            # === FOG/WEATHER DETECTION ===
            is_fog, fog_details = self.detect_fog_conditions(frame)
            is_gradual_change = self.detect_weather_gradual_change()
            
            # Update history for weather tracking
            self.brightness_history.append(current_brightness)
            self.contrast_history.append(fog_details['contrast_ratio'])
            
            # If fog detected, pause certain alerts
            if is_fog or is_gradual_change:
                self.weather_pause_until = time.time() + 5
                weather_status = "FOG DETECTED" if is_fog else "GRADUAL CHANGE"
                cv2.putText(frame, weather_status, (10, 320), 
                           cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
                cv2.putText(frame, "WEATHER MODE - ALERTS ADJUSTED", (10, 360), 
                           cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
            
            # === DETECTION METHODS (Weather-Aware) ===
            current_time = time.time()
            weather_mode = current_time < self.weather_pause_until
            
            # 1. Overall Blur Detection (adjusted for weather)
            blur_score = self.calculate_blur(frame)
            blur_threshold_adjusted = self.blur_threshold * 0.5 if weather_mode else self.blur_threshold
            blur_alert = blur_score < blur_threshold_adjusted
            blur_color = (0, 0, 255) if blur_alert else (0, 255, 0)
            cv2.putText(frame, f"Blur: {blur_score:.0f}", (10, 40), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.8, blur_color, 2)
            
            if blur_alert and not weather_mode:
                self.trigger_alert('blur', f'Score: {blur_score:.1f}')
            
            # 2. Darkness Detection (fog-aware)
            is_covered, dark_ratio = self.check_dark_coverage(frame)
            dark_alert = is_covered and not is_fog
            dark_color = (0, 0, 255) if dark_alert else (0, 255, 0)
            cv2.putText(frame, f"Dark: {dark_ratio*100:.0f}%", (10, 80), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.8, dark_color, 2)
            
            if dark_alert:
                self.trigger_alert('darkness', f'{dark_ratio*100:.0f}% dark coverage')
            
            # 3. Color Dominance Detection
            color_dominant = self.is_color_dominant(frame)
            color_color = (0, 0, 255) if color_dominant else (0, 255, 0)
            cv2.putText(frame, f"Color: {'ALERT' if color_dominant else 'OK'}", (10, 120), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.8, color_color, 2)
            
            if color_dominant:
                self.trigger_alert('color', 'Red dominance detected')
            
            # 4. Blurry Tiles Detection (adjusted for weather)
            is_blurry_tiles, tile_ratio = self.detect_blurry_tiles(frame)
            tile_threshold_adjusted = self.tile_blur_ratio * 1.5 if weather_mode else self.tile_blur_ratio
            tile_alert = tile_ratio > tile_threshold_adjusted
            tile_color = (0, 0, 255) if tile_alert else (0, 255, 0)
            cv2.putText(frame, f"Tiles: {tile_ratio*100:.0f}% blurry", (10, 160), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.8, tile_color, 2)
            
            if tile_alert and not weather_mode:
                self.trigger_alert('tiles', f'{tile_ratio*100:.0f}% tiles blurry')
            
            # 5. Brightness Change Detection
            brightness_ratio = current_brightness / self.ref_brightness if self.ref_brightness != 0 else 1
            if brightness_ratio < 0.7 or brightness_ratio > 1.3:
                if not weather_mode:
                    self.brightness_pause_until = time.time() + 2
                cv2.putText(frame, "LIGHTING CHANGE - SSIM PAUSED", (10, 200), 
                           cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
            
            # 6. SSIM Detection (paused during weather)
            now = time.time()
            if now - self.last_ssim_check > self.ssim_check_interval:
                ssim_score = self.calculate_ssim_score(gray)
                self.ssim_window.append(ssim_score)
                self.last_ssim_check = now
                
                ssim_color = (0, 255, 0) if ssim_score > self.ssim_threshold else (255, 255, 0)
                cv2.putText(frame, f"SSIM: {ssim_score:.3f}", (10, 240), 
                           cv2.FONT_HERSHEY_SIMPLEX, 0.8, ssim_color, 2)
                
                # Check for persistent SSIM alerts (skip during weather)
                pause_condition = self.brightness_pause_until if not weather_mode else self.weather_pause_until
                if now > pause_condition and len(self.ssim_window) == 6:
                    low_ssim_count = sum(1 for score in self.ssim_window if score < self.ssim_threshold)
                    if low_ssim_count >= 5 and not weather_mode:
                        self.trigger_alert('ssim', 'Persistent scene change')
                        cv2.putText(frame, "SCENE CHANGE DETECTED", (10, 280), 
                                   cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
            
            # Weather information display
            if is_fog:
                cv2.putText(frame, f"Fog Score: {fog_details['fog_score']}/3", (10, 400), 
                           cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)
                cv2.putText(frame, f"Contrast: {fog_details['contrast_ratio']:.2f}", (10, 420), 
                           cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)
            
            # Processing info
            processing_time = (time.time() - start_time) * 1000
            cv2.putText(frame, f"Speed: {processing_time:.1f}ms | Frame: {frame_count}", 
                       (10, frame.shape[0] - 40), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
            
            # Stream info
            cv2.putText(frame, "HYBRID: ChatGPT + Claude Methods", 
                       (10, frame.shape[0] - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
            
            # Display frame
            cv2.imshow('Hybrid Monitor', frame)
            
            # Controlsq
            key = cv2.waitKey(1) & 0xFF
            if key == ord('q'): # Close program
                self.running = False
            elif key == ord('r'):
                print("📸 Capturing new reference...")
                self.capture_reference_frame()
                self.ssim_window.clear()


            # # Add block of code to limit fps so CPU is not overburdened (?)
            # target_fps = 0.1        # trying between 1 - 5, no real changes
            # frame_time = 1.0 / target_fps
            # elapsed = time.time() - start_time
            # if elapsed < frame_time:
            #     time.sleep(frame_time - elapsed)
        
        self.cleanup()
    
    def cleanup(self):
        """Clean up resources"""
        self.running = False
        if hasattr(self, 'cap'):
            self.cap.release()
        cv2.destroyAllWindows()
        print("🛑 Hybrid monitor stopped")

def main():
    # 1) Ask the user for a camera source + desired resolution
    selection = pick_camera_source()
    if selection is None:
        print("No source selected. Exiting.")
        return

    # Backward/forward compatibility: allow int/str or {"source": ..., "resolution": ...}
    if isinstance(selection, dict):
        src = selection.get("source")
        desired_res = selection.get("resolution")  # (w, h) or None
    else:
        src = selection
        desired_res = None

    # 2) Optional: add low-latency flags when using RTSP
    if isinstance(src, str) and src.startswith("rtsp://"):
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
            "rtsp_transport;udp|fflags;nobuffer|flags;low_delay|"
            "reorder_queue_size;0|max_delay;500000|stimeout;2000000"
        )

    # 3) Start the monitor with the chosen source
    monitor = HybridCameraQualityMonitor(camera_source=src)
    # attach desired resolution without changing the class signature
    monitor.desired_resolution = desired_res

    try:
        monitor.run_monitoring()
    except KeyboardInterrupt:
        print("\nStopping...")
        monitor.cleanup()
    except Exception as e:
        print(f"Error: {e}")
        monitor.cleanup()



if __name__ == "__main__":
    main()