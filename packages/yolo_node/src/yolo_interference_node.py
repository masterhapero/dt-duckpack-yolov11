#!/usr/bin/env python3

import asyncio
from typing import Optional

import cv2
import numpy as np
import time
import queue
import threading

from ultralytics import YOLO

import rospy
from duckietown.dtros import DTROS, NodeType, TopicType
# from hardware_test_camera import HardwareTestCamera
from sensor_msgs.msg import CompressedImage as ROSCompressedImage

from dt_robot_utils import get_robot_name
from dtps import context, ContextConfig
from dtps_http import RawData
from duckietown_messages.sensors.camera import Camera
from duckietown_messages.sensors.compressed_image import CompressedImage
from duckietown_messages.utils.exceptions import DataDecodingError

class YoloNode(DTROS):
    """
    Interference frames from a DTPS source and publish as JPEG

    Publisher:
        ~yolo/compressed (:obj:`CompressedImage`): The YoloV11 interference frames
    """

    def __init__(self, camera_name: str = "front_center"):
        # Initialize the DTROS parent class
        super(YoloNode, self).__init__(
            node_name="yolo",
            node_type=NodeType.DRIVER,
            help="Reads a stream of images from a camera and interference with YoloV11",
        )
        self._robot_name = get_robot_name()
        self._camera_name = camera_name
        # Setup publishers
        self._has_published: bool = False
        self.pub_img = rospy.Publisher(
            "~yolo/compressed",
            ROSCompressedImage,
            queue_size=1,
            dt_topic_type=TopicType.DRIVER,
            dt_help="The stream of JPEG compressed images from the yolo",
        )
        self.time = rospy.Time.now()
        model_path = "/code/src/dt-duckpack-yolo/packages/yolo_node/best.engine"
        print(f"Loading YOLO model from {model_path}")
        self.model = YOLO(model_path, task="detect")
        # Dummy frame to initialize pipeline
        #frame = np.random.choice(np.arange(100, dtype=np.uint8), size=(3, 640, 480))
        #frame = np.zeros((3, 640, 480), dtype=np.uint8)
        #print("Attempt to predict")
        #results = self.model.predict(frame, imgsz=480)
        #print("Attempt to predict complete")
        self.frame_queue = queue.Queue(maxsize=1)  # Only keep latest frame
        self.processor_thread = threading.Thread(target=self.process_frames)
        self.processor_thread.daemon = True
        self.processor_thread.start()
        self.loginfo("Initialized.")

    async def publish(self, data: RawData):
        # Update the timestamp
        ros_time = rospy.Time.now()
        jpeg: CompressedImage = CompressedImage.from_rawdata(data)
        frame = cv2.imdecode(
                    np.frombuffer(jpeg.data, np.uint8),
                    cv2.IMREAD_COLOR)
        try:
            while not self.frame_queue.empty():
                _ = self.frame_queue.get_nowait()
               # print("Dropped old frame")
        except queue.Empty:
            pass

        # Add timestamp to frame data
        frame_with_timestamp = {
            'frame': frame,
            'timestamp': ros_time,
        }
        self.frame_queue.put(frame_with_timestamp)
        # print("Enqueued frame");

    def process_frames(self):
    	while True:
            try:
                print("Waiting for frame")
                frame_data = self.frame_queue.get(timeout=1.0)
                frame = frame_data['frame']
                timestamp = frame_data['timestamp']
                process_start = time.time()
                processed_frame = frame.copy()
                results = self.model.predict(frame, imgsz=480)
	            # Process results
                detections = []
                for r in results:
                    boxes = r.boxes
                    for box in boxes:
                        detection = {
                            'bbox': box.xyxy[0].tolist(),
                            'conf': float(box.conf),
                            'cls': int(box.cls)
                        }
                        detections.append(detection)
                # Visualize results on the copy
                for det in detections:
                    bbox = det['bbox']
                    x1, y1, x2, y2 = map(int, bbox)
                    cv2.rectangle(processed_frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                    cv2.putText(
                        processed_frame,
                        f"Class {det['cls']}: {det['conf']:.2f}",
                        (x1, y1 - 10),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.5,
                        (0, 255, 0),
                        2
                        )
	    	# create CompressedImage message
                msg: RosCompressedImage = ROSCompressedImage()
                msg.header.stamp = timestamp
                msg.format = "jpeg"
                msg.data = np.array(cv2.imencode('.jpg', processed_frame)[1]).tostring()
                # publish image
                self.pub_img.publish(msg)
                print("published yolo")
            except queue.Empty:
                continue
            except Exception as e:
                print(f"Error while interference {e}")

    async def worker(self):
        # create switchboard context
        switchboard = (await context("switchboard")).navigate(self._robot_name)
        # wait for camera to be ready
        jpeg = await (switchboard / "sensor" / "camera" / self._camera_name / "jpeg").until_ready()
    	# Enable dynamic reconnection to the topic
        jpeg = jpeg.configure(ContextConfig(patient=True))
        # subscribe
        await jpeg.subscribe(self.publish)
        # ---
        await self.join()

    async def join(self):
        while not self.is_shutdown:
            await asyncio.sleep(1)

    def spin(self):
        try:
            asyncio.run(self.worker())
        except RuntimeError:
            if not self.is_shutdown:
                self.logerr("An error occurred while running the event loop")
                raise

if __name__ == "__main__":
    # initialize the node
    yolo_node = YoloNode()
    # keep the node alive
    yolo_node.spin()

