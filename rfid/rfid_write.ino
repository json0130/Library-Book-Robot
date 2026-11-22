#include <SPI.h>
#include <MFRC522.h>
 
#define SS_PIN 10
#define RST_PIN 9
MFRC522 mfrc522(SS_PIN, RST_PIN);   // Create MFRC522 instance.

void setup()
{
    Serial.begin(9600);   // Initiate a serial communication
    SPI.begin();          // Initiate  SPI bus
    mfrc522.PCD_Init();   // Initiate MFRC522
}

void loop() 
{
    // Look for new cards
    if (!mfrc522.PICC_IsNewCardPresent()) {
        return;
    }

    // Select one of the cards
    if (!mfrc522.PICC_ReadCardSerial()) {
        return;
    }
    
    // Show UID on serial monitor
    String content= "";
    byte letter;
    for (byte i = 0; i < mfrc522.uid.size; i++) {
        content.concat(String(mfrc522.uid.uidByte[i] < 0x10 ? " 0" : " "));
        content.concat(String(mfrc522.uid.uidByte[i], HEX));
    }
    content.toUpperCase();
    // timezone 04 65 B2 42 CD 21 90 "CLEAN CODE"
    // gameon 1D 5C 7B 36 "DUNE"
    if (content.substring(1) == "04 65 B2 42 CD 21 90")
    {
        Serial.write("CLEAN CODE\n");
        delay(1000);
    } else if (content.substring(1) == "1D 5C 7B 36") {
        Serial.write("DUNE\n");
        delay(1000);
    }
}