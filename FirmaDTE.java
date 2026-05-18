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
import org.xml.sax.InputSource;
import java.security.*;
import java.security.cert.*;
import java.util.*;
import java.util.Base64;

public class FirmaDTE {

    public static void main(String[] args) throws Exception {
        if (args.length < 4) {
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

        // Parsear XML — forzar ISO-8859-1
        DocumentBuilderFactory dbf = DocumentBuilderFactory.newInstance();
        dbf.setNamespaceAware(true);
        DocumentBuilder builder = dbf.newDocumentBuilder();

        // Convertir bytes ISO-8859-1 a string y luego parsear como UTF-8
        // El XML declara encoding="ISO-8859-1" — Java lo maneja correctamente
        // si usamos InputSource con el Reader correcto
        InputSource is = new InputSource(new InputStreamReader(
            new ByteArrayInputStream(xmlBytes), "ISO-8859-1"
        ));
        Document doc = builder.parse(is);

        if (modo.equals("firmar-dte")) {
            if (args.length < 5) {
                System.err.println("firmar-dte requiere doc_id");
                System.exit(1);
            }
            String docId = args[4];
            firmarDTE(doc, privKey, cert, docId);
        } else if (modo.equals("firmar-sobre")) {
            firmarSobre(doc, privKey, cert);
        } else {
            System.err.println("Modo desconocido: " + modo);
            System.exit(1);
        }

        // Serializar en ISO-8859-1
        ByteArrayOutputStream baos = new ByteArrayOutputStream();
        Transformer t = TransformerFactory.newInstance().newTransformer();
        t.setOutputProperty(OutputKeys.ENCODING, "ISO-8859-1");
        t.setOutputProperty(OutputKeys.OMIT_XML_DECLARATION, "yes");
        t.transform(new DOMSource(doc), new StreamResult(baos));

        // Agregar declaracion XML sin standalone
        byte[] xmlDecl = "<?xml version=\"1.0\" encoding=\"ISO-8859-1\"?>\n".getBytes("ISO-8859-1");
        byte[] xmlBody = baos.toByteArray();
        byte[] result = new byte[xmlDecl.length + xmlBody.length];
        System.arraycopy(xmlDecl, 0, result, 0, xmlDecl.length);
        System.arraycopy(xmlBody, 0, result, xmlDecl.length, xmlBody.length);

        System.out.print(Base64.getEncoder().encodeToString(result));
    }

    static void firmarDTE(Document doc, PrivateKey privKey,
                           X509Certificate cert, String docId) throws Exception {
        XMLSignatureFactory fac = XMLSignatureFactory.getInstance("DOM");

        // Usar enveloped-signature como la libreria oficial NIC Chile
        List<Transform> transforms = Collections.singletonList(
            fac.newTransform(
                Transform.ENVELOPED,
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

        // Firma va dentro del EnvioDTE
        Element envioEl = doc.getDocumentElement();
        DOMSignContext dsc = new DOMSignContext(privKey, envioEl);
        signature.sign(dsc);
    }
}
