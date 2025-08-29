"""
TrashMinder - An AppDaemon app to monitor trash bin placement using computer vision

This app:
- Captures images from camera.front_yard every hour during the monitoring window
- Uses GPT-4o to analyze if the trash bin is near the street
- Sends Pushover notifications if the trash bin is not detected
- Only operates from 3pm Wednesday to 9am Thursday
"""

import appdaemon.plugins.hass.hassapi as hass
import requests
import base64
import json
from datetime import datetime, timedelta
from openai import OpenAI


class TrashMinder(hass.Hass):
    """AppDaemon app to monitor trash bin placement"""
    
    def initialize(self):
        """Initialize the TrashMinder app"""
        
        # Get configuration parameters
        self.camera_entity = self.args.get("camera_entity", "camera.front_yard")
        self.openai_api_key = self.args.get("openai_api_key")
        self.pushover_user_key = self.args.get("pushover_user_key")
        self.pushover_api_token = self.args.get("pushover_api_token")
        self.test_mode = self.args.get("test_mode", False)
        
        # Debug logging (sanitized - no secrets)
        sanitized_args = {k: "***REDACTED***" if "key" in k.lower() or "token" in k.lower() else v for k, v in self.args.items()}
        self.log(f"DEBUG: Args received (secrets redacted): {sanitized_args}")
        self.log(f"DEBUG: test_mode value: {self.test_mode}, type: {type(self.test_mode)}")
        
        # Validate required configuration
        if not self.openai_api_key:
            self.log("ERROR: openai_api_key is required", level="ERROR")
            return
        
        if not self.pushover_user_key or not self.pushover_api_token:
            self.log("ERROR: pushover_user_key and pushover_api_token are required", level="ERROR")
            return
            
        # Initialize OpenAI client
        self.openai_client = OpenAI(api_key=self.openai_api_key)
            
        # Set up the monitoring schedule
        self.setup_monitoring_schedule()
        
        if self.test_mode:
            self.log("TrashMinder initialized in TEST MODE - will check every minute")
        else:
            self.log("TrashMinder initialized successfully - normal weekly schedule")
    
    def setup_monitoring_schedule(self):
        """Set up the monitoring schedule based on mode"""
        
        if self.test_mode:
            # In test mode, run every minute
            self.run_every(
                self.check_trash_bin_test,
                datetime.now() + timedelta(seconds=10),
                60  # Run every 60 seconds
            )
            self.log("TEST MODE: Monitoring every 60 seconds starting in 10 seconds")
        else:
            # Normal mode: Schedule to start monitoring at 3pm every Wednesday
            # Using run_daily with constrain_days for Wednesday only
            self.run_daily(
                self.start_monitoring,
                "15:00:00",  # 3pm
                constrain_days="wed"
            )
            self.log("Monitoring schedule set up - will start at 3pm every Wednesday")
    
    def check_trash_bin_test(self, kwargs):
        """Test mode version that runs every minute"""
        self.log("TEST MODE: Running trash bin check")
        # Call the regular check function
        self.check_trash_bin(kwargs)
    
    def start_monitoring(self, kwargs):
        """Start the hourly monitoring cycle"""
        
        self.log("Starting trash bin monitoring cycle")
        
        # Schedule hourly checks for the next 18 hours (3pm Wed to 9am Thu)
        for hour_offset in range(18):  # 18 hours from 3pm Wed to 9am Thu
            check_time = datetime.now() + timedelta(hours=hour_offset)
            
            # Stop at 9am Thursday
            if check_time.weekday() == 3 and check_time.hour >= 9:  # Thursday and 9am or later
                break
                
            # Use run_in with proper random parameter (single random value in seconds)
            self.run_in(
                self.check_trash_bin,
                hour_offset * 3600,  # Convert hours to seconds
                random_start=-300,  # Random offset up to 5 minutes before/after
                random_end=300
            )
        
        self.log(f"Scheduled 18 hourly trash bin checks starting now")
    
    def check_trash_bin(self, kwargs):
        """Check if trash bin is near the street"""
        
        self.log("Starting trash bin check...")
        
        try:
            # Capture image from camera
            self.log(f"Capturing image from camera: {self.camera_entity}")
            image_data = self.capture_camera_image()
            if not image_data:
                self.log("Failed to capture camera image", level="WARNING")
                return
            
            self.log("Image captured successfully, analyzing with GPT-4o...")
            
            # Analyze image with GPT-4o
            analysis_result = self.analyze_image_with_gpt(image_data)
            trash_bin_detected = analysis_result["detected"]
            confidence = analysis_result["confidence"]
            description = analysis_result["description"]
            
            # Log the results
            if self.test_mode:
                self.log(f"TEST MODE RESULT: Detected={trash_bin_detected}, Confidence={confidence}, Description={description}")
            
            # Send notification if trash bin not detected
            if not trash_bin_detected:
                self.send_pushover_notification(confidence, description, image_data)
                self.log(f"Trash bin not detected (confidence: {confidence}) - notification sent: {description}")
            else:
                self.log(f"Trash bin detected near street (confidence: {confidence}) - all good! {description}")
                # In test mode, send notification even when detected (to test image attachment)
                if self.test_mode:
                    self.send_test_notification(confidence, description, image_data)
                
        except Exception as e:
            self.log(f"Error during trash bin check: {str(e)}", level="ERROR")
            if self.test_mode:
                import traceback
                self.log(f"TEST MODE - Full error trace: {traceback.format_exc()}", level="ERROR")
    
    def capture_camera_image(self):
        """Capture image from the specified camera entity"""
        
        try:
            import requests
            import os
            
            # Get the Home Assistant token
            ha_token = os.environ.get('SUPERVISOR_TOKEN')
            if not ha_token:
                self.log("No SUPERVISOR_TOKEN found", level="ERROR")
                return None
            
            # Make direct API call to get camera image
            url = f"http://supervisor/core/api/camera_proxy/{self.camera_entity}"
            headers = {
                "Authorization": f"Bearer {ha_token}",
                "Content-Type": "application/json"
            }
            
            self.log(f"Fetching image from camera API: {url}")
            response = requests.get(url, headers=headers, timeout=10)
            
            if response.status_code == 200:
                image_data = response.content
                self.log(f"Successfully captured image ({len(image_data)} bytes)")
                return image_data
            else:
                self.log(f"Camera API returned status {response.status_code}: {response.text}", level="ERROR")
                return None
            
        except Exception as e:
            self.log(f"Error capturing camera image: {str(e)}", level="ERROR")
            return None
    
    def analyze_image_with_gpt(self, image_data):
        """Analyze the image using GPT-4o to detect trash bin placement"""
        
        try:
            # Encode image to base64
            image_base64 = base64.b64encode(image_data).decode('utf-8')
            
            # Use OpenAI client to analyze the image with structured JSON output
            response = self.openai_client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": """Analyze this image and determine if there is a trash bin/garbage can positioned near or at the street/curb for pickup.

Look for:
- Wheeled garbage bins or trash cans
- Bins positioned at or near the curb/street edge
- Typical residential waste containers

Return a JSON response with a boolean indicating whether a trash bin is clearly visible and positioned near the street for pickup (not just anywhere in the image, but specifically positioned for collection)."""
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{image_base64}"
                                }
                            }
                        ]
                    }
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "trash_bin_detection",
                        "schema": {
                            "type": "object",
                            "properties": {
                                "trash_bin_present": {
                                    "type": "boolean",
                                    "description": "True if a trash bin is clearly visible and positioned near the street/curb for pickup, False otherwise"
                                },
                                "confidence": {
                                    "type": "string",
                                    "enum": ["high", "medium", "low"],
                                    "description": "Confidence level of the detection"
                                },
                                "description": {
                                    "type": "string",
                                    "description": "Brief description of what was observed"
                                }
                            },
                            "required": ["trash_bin_present", "confidence", "description"],
                            "additionalProperties": False
                        },
                        "strict": True
                    }
                },
                max_tokens=100
            )
            
            # Parse the JSON response
            result = json.loads(response.choices[0].message.content)
            
            trash_bin_detected = result["trash_bin_present"]
            confidence = result["confidence"]
            description = result["description"]
            
            self.log(f"GPT-4o analysis result: trash_bin_present={trash_bin_detected}, confidence={confidence}, description='{description}'")
            
            # Return structured result
            return {
                "detected": trash_bin_detected,
                "confidence": confidence,
                "description": description
            }
                
        except Exception as e:
            self.log(f"Error analyzing image with GPT: {str(e)}", level="ERROR")
            # Return safe default on error to avoid false alarms
            return {
                "detected": True,
                "confidence": "low", 
                "description": f"Analysis failed: {str(e)}"
            }
    
    def send_pushover_notification(self, confidence="unknown", description="", image_data=None):
        """Send notification via Pushover that trash bin is not detected"""
        
        try:
            current_time = datetime.now().strftime("%I:%M %p")
            
            # Create detailed message with AI analysis info
            message = f"Trash bin not detected near the street as of {current_time}.\n\n"
            message += f"AI Analysis: {description}\n"
            message += f"Confidence: {confidence.title()}\n\n"
            message += "Don't forget to put it out for pickup!"
            
            payload = {
                "token": self.pushover_api_token,
                "user": self.pushover_user_key,
                "title": "🗑️ Trash Bin Reminder",
                "message": message,
                "priority": 1,  # High priority
                "sound": "pushover"
            }
            
            # Prepare files for attachment if image data is provided
            files = None
            if image_data:
                files = {
                    "attachment": ("camera_snapshot.jpg", image_data, "image/jpeg")
                }
                self.log("Including camera image with Pushover notification")
            
            response = requests.post(
                "https://api.pushover.net/1/messages.json",
                data=payload,
                files=files,
                timeout=10
            )
            
            if response.status_code == 200:
                self.log("Pushover notification sent successfully")
            else:
                self.log(f"Pushover notification failed: {response.status_code} - {response.text}", level="ERROR")
                
        except Exception as e:
            self.log(f"Error sending Pushover notification: {str(e)}", level="ERROR")
    
    def send_test_notification(self, confidence="unknown", description="", image_data=None):
        """Send a test notification in test mode to verify image attachment works"""
        
        try:
            current_time = datetime.now().strftime("%I:%M %p")
            
            # Create test message
            message = f"🧪 TEST MODE: Trash bin detected at {current_time}.\n\n"
            message += f"AI Analysis: {description}\n"
            message += f"Confidence: {confidence.title()}\n\n"
            message += "This is a test notification with camera image attached."
            
            payload = {
                "token": self.pushover_api_token,
                "user": self.pushover_user_key,
                "title": "🧪 TrashMinder Test",
                "message": message,
                "priority": 0,  # Normal priority for tests
                "sound": "pushover"
            }
            
            # Prepare files for attachment if image data is provided
            files = None
            if image_data:
                files = {
                    "attachment": ("test_camera_snapshot.jpg", image_data, "image/jpeg")
                }
                self.log("Including camera image with test Pushover notification")
            
            response = requests.post(
                "https://api.pushover.net/1/messages.json",
                data=payload,
                files=files,
                timeout=10
            )
            
            if response.status_code == 200:
                self.log("Test Pushover notification sent successfully")
            else:
                self.log(f"Test Pushover notification failed: {response.status_code} - {response.text}", level="ERROR")
            
        except Exception as e:
            self.log(f"Error sending test Pushover notification: {str(e)}", level="ERROR")
    
    def terminate(self):
        """Clean up when app is terminated"""
        self.log("TrashMinder app terminated")
