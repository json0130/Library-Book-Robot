// RFID tag reader for the library robot: sends the number of every tag held to the reader, one line per tag, over USB serial.
//
//     UID 04 65 B2 42 CD 21 90
//
// The robot (robot/perception/rfid.py) looks the number up in data/rfid_tags.csv to find the book, so a new book needs no change here:
// register its tag with  python tools/rfid_register.py.  (rfid_write.ino, the first version, had the titles written into the code.)
//
// Wiring, Arduino Uno -> MFRC522:  SDA/SS D10, SCK D13, MOSI D11, MISO D12, RST D9, 3.3V, GND (IRQ not connected). 9600 baud.
// Library: "MFRC522" by GithubCommunity (Arduino IDE: Sketch > Include Library > Manage Libraries).

#include <SPI.h>
#include <MFRC522.h>

#define SS_PIN 10
#define RST_PIN 9
MFRC522 mfrc522(SS_PIN, RST_PIN);

const unsigned long REPEAT_MS = 1000;   // a tag that stays on the reader is sent again this often (the robot counts it as one book)

void setup() {
  Serial.begin(9600);
  SPI.begin();
  mfrc522.PCD_Init();
}

void loop() {
  if (!mfrc522.PICC_IsNewCardPresent() || !mfrc522.PICC_ReadCardSerial()) {
    return;
  }
  Serial.print("UID");
  for (byte i = 0; i < mfrc522.uid.size; i++) {
    Serial.print(mfrc522.uid.uidByte[i] < 0x10 ? " 0" : " ");
    Serial.print(mfrc522.uid.uidByte[i], HEX);
  }
  Serial.println();
  mfrc522.PICC_HaltA();                 // stop talking to this card until it is taken away and held again
  mfrc522.PCD_StopCrypto1();
  delay(REPEAT_MS);
}
