import unittest
import numpy as np
import cv2
from visionguard.main import HybridCameraQualityMonitor 


class TestHybridCameraQualityMonitor(unittest.TestCase):
    def setUp(self):
        # Create a dummy instance (no real camera)
        self.monitor = HybridCameraQualityMonitor(camera_source=0, save_alerts=False)

        # Create synthetic test images
        self.clear_image = np.full((100, 100, 3), 255, dtype=np.uint8)  # bright white
        self.dark_image = np.full((100, 100, 3), 10, dtype=np.uint8)    # dark
        self.blurry_image = cv2.GaussianBlur(self.clear_image, (15, 15), 0)




    #----------------Dummy ChatGPT tests - replace later.------------------------
    def test_check_dark_coverage(self):
        is_dark, dark_ratio = self.monitor.check_dark_coverage(self.dark_image)
        self.assertTrue(is_dark, "Dark image should be detected as dark")
        self.assertGreater(dark_ratio, 0.3)

    def test_detect_fog_conditions(self):
        fog, details = self.monitor.detect_fog_conditions(self.clear_image)
        self.assertIn('brightness', details)
        self.assertIsInstance(fog, bool)
    #-----------------------------------------------------------------------------

if __name__ == '__main__':
    unittest.main()