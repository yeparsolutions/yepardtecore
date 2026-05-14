import javax.xml.crypto.*;
import javax.xml.crypto.dsig.*;
import javax.xml.crypto.dsig.dom.*;
import javax.xml.crypto.dsig.keyinfo.*;
import javax.xml.crypto.dsig.spec.*;
import javax.xml.parsers.*;
import javax.xml.transform.*;
import javax.xml.transform.dom.*;
import javax.xml.transform.stream.*;
import org.w3c.dom.*;
import java.io.*;
import java.security.*;
import java.security.cert.*;
import java.util.*;
import java.util.Base64;

/**
 * FirmaDTE.java — Firma XMLDSig para SII Chile
 *
 * Modos:
 *   firmar-dte  <xml_b64> <pfx_b64> <password> <doc_id>
 *   firmar-sobre <xml_b64> <pfx_b64> <password>
 *
 * Salida: XML firmado en Base64 por stdout
 */
public class FirmaDTE {

    public static void main(String[] args) throws Exception {
        if (args.length < 3) {
            System.err.println("Uso: java FirmaDTE <modo> <xml_b64> <pfx_b64> <password> [doc_id]");
            System.exit(1);
        }

        String modo     = args[0];
        byte[] xmlBytes = Base64.getDecoder().decode(args[1]);
        byte[] pfxBytes = Base64.getDecoder().decode(args[2]);
        String password = args[3];

        // Cargar certificado
        KeyStore ks = KeyStore.getInstance("PKCS12");
        ks.load(new ByteArrayInputStream(pfxBytes), password.toCharArray());
        String alias      = ks.aliases().nextElement();
        PrivateKey privKey = (PrivateKey) ks.getKey(alias, password.toCharArray());
        X509Certificate cert = (X509Certificate) ks.getCertificate(alias);

        // Parsear XML
        DocumentBuilderFactory dbf = DocumentBuilderFactory.newInstance();
        dbf.setNamespaceAware(true);
        Document doc = dbf.newDocumentBuilder()
                          .parse(new ByteArrayInputStream(xmlBytes));

        if (modo.equals("firmar-dte")) {
            String docId = args[4];
            firmarDTE(doc, privKey, cert, docId);
        } else if (modo.equals("firmar-sobre")) {
            firmarSobre(doc, privKey, cert);
        } else {
            System.err.println("Modo desconocido: " + modo);
            System.exit(1);
        }

        // Serializar
        ByteArrayOutputStream baos = new ByteArrayOutputStream();
        Transformer t = TransformerFactory.newInstance().newTransformer();
        t.setOutputProperty(OutputKeys.ENCODING, "ISO-8859-1");
        t.setOutputProperty(OutputKeys.OMIT_XML_DECLARATION, "no");
        t.transform(new DOMSource(doc), new StreamResult(baos));

        System.out.print(Base64.getEncoder().encodeToString(baos.toByteArray()));
    }

    static void firmarDTE(Document doc, PrivateKey privKey,
                           X509Certificate cert, String docId) throws Exception {
        XMLSignatureFactory fac = XMLSignatureFactory.getInstance("DOM");

        // Transform c14n
        List<Transform> transforms = Collections.singletonList(
            fac.newTransform(
                "http://www.w3.org/TR/2001/REC-xml-c14n-20010315",
                (TransformParameterSpec) null
            )
        );

        Reference ref = fac.newReference(
            "#" + docId,
            fac.newDigestMethod(DigestMethod.SHA1, null),
            transforms, null, null
        );

        SignedInfo si = fac.newSignedInfo(
            fac.newCanonicalizationMethod(
                "http://www.w3.org/TR/2001/REC-xml-c14n-20010315",
                (C14NMethodParameterSpec) null
            ),
            fac.newSignatureMethod(SignatureMethod.RSA_SHA1, null),
            Collections.singletonList(ref)
        );

        KeyInfoFactory kif = fac.getKeyInfoFactory();
        KeyValue kv   = kif.newKeyValue(cert.getPublicKey());
        X509Data x509 = kif.newX509Data(Collections.singletonList(cert));
        KeyInfo ki    = kif.newKeyInfo(Arrays.asList(kv, x509));

        XMLSignature signature = fac.newXMLSignature(si, ki);

        // Registrar ID del Documento
        NodeList docNodes = doc.getElementsByTagNameNS(
            "http://www.sii.cl/SiiDte", "Documento");
        if (docNodes.getLength() == 0)
            docNodes = doc.getElementsByTagName("Documento");
        ((Element) docNodes.item(0)).setIdAttribute("ID", true);

        // Insertar firma dentro del DTE
        NodeList dteNodes = doc.getElementsByTagNameNS(
            "http://www.sii.cl/SiiDte", "DTE");
        if (dteNodes.getLength() == 0)
            dteNodes = doc.getElementsByTagName("DTE");
        Element dteEl = (Element) dteNodes.item(0);

        DOMSignContext dsc = new DOMSignContext(privKey, dteEl);
        signature.sign(dsc);
    }

    static void firmarSobre(Document doc, PrivateKey privKey,
                             X509Certificate cert) throws Exception {
        XMLSignatureFactory fac = XMLSignatureFactory.getInstance("DOM");

        // Transform c14n para el SetDTE
        List<Transform> transforms = Collections.singletonList(
            fac.newTransform(
                "http://www.w3.org/TR/2001/REC-xml-c14n-20010315",
                (TransformParameterSpec) null
            )
        );

        Reference ref = fac.newReference(
            "#SetDoc",
            fac.newDigestMethod(DigestMethod.SHA1, null),
            transforms, null, null
        );

        SignedInfo si = fac.newSignedInfo(
            fac.newCanonicalizationMethod(
                "http://www.w3.org/TR/2001/REC-xml-c14n-20010315",
                (C14NMethodParameterSpec) null
            ),
            fac.newSignatureMethod(SignatureMethod.RSA_SHA1, null),
            Collections.singletonList(ref)
        );

        KeyInfoFactory kif = fac.getKeyInfoFactory();
        KeyValue kv   = kif.newKeyValue(cert.getPublicKey());
        X509Data x509 = kif.newX509Data(Collections.singletonList(cert));
        KeyInfo ki    = kif.newKeyInfo(Arrays.asList(kv, x509));

        XMLSignature signature = fac.newXMLSignature(si, ki);

        // Registrar ID del SetDTE
        NodeList setNodes = doc.getElementsByTagNameNS(
            "http://www.sii.cl/SiiDte", "SetDTE");
        if (setNodes.getLength() == 0)
            setNodes = doc.getElementsByTagName("SetDTE");
        ((Element) setNodes.item(0)).setIdAttribute("ID", true);

        // Insertar firma dentro del EnvioDTE (hermano del SetDTE)
        Element envioEl = doc.getDocumentElement();
        DOMSignContext dsc = new DOMSignContext(privKey, envioEl);
        signature.sign(dsc);
    }
}
