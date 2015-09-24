#!/usr/bin/env python2
#
# Copyright 2015 Carnegie Mellon University
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
sys.path.append(".")
sys.path.append("/Users/bamos/src/dlib/python_examples")
# Abusing the path here to run on multiple machines without modifications.
# Also this shows a bad example of using multiple dlib versions, don't do this.
sys.path.append("/home/bamos/src/dlib-18.15/python_examples")
sys.path.append("/home/ubuntu/src/dlib-18.16/python_examples")
sys.path.append("/home/ubuntu/src/caffe/python")

from autobahn.twisted.websocket import WebSocketServerProtocol, \
    WebSocketServerFactory
from twisted.python import log
from twisted.internet import reactor

import cv2
import imagehash
import json
from PIL import Image
import numpy as np
import os
import StringIO
import urllib, base64

from sklearn.decomposition import PCA
from sklearn.manifold import TSNE
from sklearn.svm import SVC

import matplotlib.pyplot as plt
import matplotlib.cm as cm

import facenet
from facenet.alignment import NaiveDlib

import tempfile

align = NaiveDlib("models/dlib/",
                        "shape_predictor_68_face_landmarks.dat")
facenet = facenet.TorchWrap('models/facenet/nn4.v1.t7', imgDim=96, cuda=False)

class Face:
    def __init__(self, rep, identity):
        self.rep = rep
        self.identity = identity

    def __repr__(self):
        return "{{id: {}, rep[0:5]: {}}}".format(
            str(self.identity),
            self.rep[0:5]
        )

class OfrServerProtocol(WebSocketServerProtocol):
    def __init__(self):
        self.images = {}
        self.training = True
        self.people = []
        self.svm = None
        self.unknownImgs = np.load("./demos/web/unknown.npy")

    def onConnect(self, request):
        print("Client connecting: {0}".format(request.peer))
        self.training = True

    def onOpen(self):
        print("WebSocket connection open.")

    def onMessage(self, payload, isBinary):
        raw = payload.decode('utf8')
        msg = json.loads(raw)
        print("Received {} message of length {}.".format(msg['type'], len(raw)))
        if msg['type'] == "ALL_STATE":
            self.loadState(msg['images'], msg['training'], msg['people'])
        elif msg['type'] == "NULL":
            self.sendMessage('{"type": "NULL"}')
        elif msg['type'] == "FRAME":
            self.processFrame(msg['dataURL'], msg['identity'])
            self.sendMessage('{"type": "PROCESSED"}')
        elif msg['type'] == "TRAINING":
            self.training = msg['val']
            if not self.training:
                self.trainSVM()
        elif msg['type'] == "ADD_PERSON":
            self.people.append(msg['val'].encode('ascii', 'ignore'))
            print(self.people)
        elif msg['type'] == "UPDATE_IDENTITY":
            h = msg['hash'].encode('ascii', 'ignore')
            if h in self.images:
                self.images[h].identity = msg['idx']
                if not self.training:
                    self.trainSVM()
            else:
                print("Image not found.")
        elif msg['type'] == "REMOVE_IMAGE":
            h = msg['hash'].encode('ascii', 'ignore')
            if h in self.images:
                del self.images[h]
                if not self.training:
                    self.trainSVM()
            else:
                print("Image not found.")
        elif msg['type'] == 'REQ_TSNE':
            self.sendTSNE(msg['people'])
        else:
            print("Warning: Unknown message type: {}".format(msg['type']))

    def onClose(self, wasClean, code, reason):
        print("WebSocket connection closed: {0}".format(reason))

    def loadState(self, jsImages, training, jsPeople):
        self.training = training

        for jsImage in jsImages:
            h = jsImage['hash'].encode('ascii', 'ignore')
            self.images[h] = Face(np.array(jsImage['representation']),
                                           jsImage['identity'])

        for jsPerson in jsPeople:
            self.people.append(jsPerson.encode('ascii', 'ignore'))

        if not training:
            self.trainSVM()

    def getData(self):
        X = []
        y = []
        for img in self.images.values():
            X.append(img.rep)
            y.append(img.identity)

        numUnknown = y.count(-1)
        numIdentified = len(y) - numUnknown
        numIdentities = len(set(y+[-1])) - 1
        if numIdentities == 0:
            return None
        numUnknownAdd = (numIdentified/numIdentities) - numUnknown
        if numUnknownAdd > 0:
            print("+ Augmenting with {} unknown images.".format(numUnknownAdd))
            for rep in self.unknownImgs[:numUnknownAdd]:
                # print(rep)
                X.append(rep)
                y.append(-1)

        X = np.vstack(X)
        y = np.array(y)
        return (X, y)

    def sendTSNE(self, people):
        d = self.getData()
        if d is None:
            return
        else:
            (X, y) = d

        X_pca = PCA(n_components=50).fit_transform(X, X)
        tsne = TSNE(n_components=2, init='random', random_state=0)
        X_r = tsne.fit_transform(X_pca)

        yVals = list(np.unique(y))
        colors = cm.rainbow(np.linspace(0, 1, len(yVals)))

        # print(yVals)

        plt.figure()
        for c, i in zip(colors, yVals):
            name = "Unknown" if i == -1 else people[i]
            plt.scatter(X_r[y == i, 0], X_r[y == i, 1], c=c, label=name)
            plt.legend()

        imgdata = StringIO.StringIO()
        plt.savefig(imgdata, format='png')
        imgdata.seek(0)

        content = 'data:image/png;base64,' + \
                  urllib.quote(base64.b64encode(imgdata.buf))
        msg = {
            "type": "TSNE_DATA",
            "content": content
        }
        self.sendMessage(json.dumps(msg))

    def trainSVM(self):
        print("+ Training SVM on {} labeled images.".format(len(self.images)))
        d = self.getData()
        if d is None:
            self.svm = None
            return
        else:
            (X, y) = d
            self.svm = SVC(kernel='rbf').fit(X, y)

    def processFrame(self, dataURL, identity):
        head = "data:image/jpeg;base64,"
        assert(dataURL.startswith(head))
        imgdata = base64.b64decode(dataURL[len(head):])
        imgF = StringIO.StringIO()
        imgF.write(imgdata)
        imgF.seek(0)
        img = Image.open(imgF)

        buf = np.fliplr(np.asarray(img))
        rgbFrame = np.zeros((300,400,3), dtype=np.uint8)
        rgbFrame[:,:,0] = buf[:,:,2]
        rgbFrame[:,:,1] = buf[:,:,1]
        rgbFrame[:,:,2] = buf[:,:,0]

        if not self.training:
            annotatedFrame = np.copy(buf)

        # cv2.imshow('frame', rgbFrame)
        # if cv2.waitKey(1) & 0xFF == ord('q'):
        #     return

        identities = []
        bbs = align.getAllFaceBoundingBoxes(rgbFrame)
        for bb in bbs:
            # print(len(bbs))
            alignedFace = align.alignImg("affine", 224, rgbFrame, bb)
            if alignedFace is None:
                continue

            phash = str(imagehash.phash(Image.fromarray(alignedFace)))
            if phash in self.images:
                identity = self.images[phash].identity
            else:
                cv2.imwrite('/tmp/facenet-web-demo.png', alignedFile)
                rep = facenet.forward("/tmp/facenet-web-demo.png")
                print(rep)
                rep = np.array(rep)
                if self.training:
                    self.images[phash] = Face(rep, identity)
                    # TODO: Transferring as a string is suboptimal.
                    content = [str(x) for x in cv2.resize(alignedFace, (0,0),
                                                          fx=0.5, fy=0.5).flatten()]
                    msg = {
                        "type": "NEW_IMAGE",
                        "hash": phash,
                        "content": content,
                        "identity": identity,
                        "representation": rep.tolist()
                    }
                    self.sendMessage(json.dumps(msg))
                else:
                    identity = self.svm.predict(rep)[0] if self.svm else -1
                    if identity not in identities:
                        identities.append(identity)

            if not self.training:
                bl = (bb.left(), bb.bottom())
                tr = (bb.right(), bb.top())
                cv2.rectangle(annotatedFrame, bl, tr, color=(153, 255, 204),
                              thickness=3)
                name = "Unknown" if identity == -1 else self.people[identity]
                cv2.putText(annotatedFrame, name, (bb.left(), bb.top()-10),
                            cv2.FONT_HERSHEY_SIMPLEX, fontScale=0.75,
                            color=(152, 255, 204), thickness=2)

        if not self.training:
            msg = {
                "type": "IDENTITIES",
                "identities": identities
            }
            self.sendMessage(json.dumps(msg))

            plt.figure()
            plt.imshow(annotatedFrame)
            plt.xticks([])
            plt.yticks([])

            imgdata = StringIO.StringIO()
            plt.savefig(imgdata, format='png')
            imgdata.seek(0)
            content = 'data:image/png;base64,' + \
                        urllib.quote(base64.b64encode(imgdata.buf))
            msg = {
                "type": "ANNOTATED",
                "content": content
            }
            self.sendMessage(json.dumps(msg))

if __name__ == '__main__':
    log.startLogging(sys.stdout)

    factory = WebSocketServerFactory("ws://localhost:9000", debug=False)
    factory.protocol = OfrServerProtocol

    reactor.listenTCP(9000, factory)
    reactor.run()
